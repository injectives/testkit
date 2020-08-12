"""
Runs all test suites in their appropriate context.
The same test suite might be executed on different Neo4j server versions or setups, it is the
responsibility of this script to setup these contexts and orchestrate which suites that
is executed in each context.
"""

import os, sys, atexit, subprocess, time, unittest, json
from datetime import datetime
from testenv import get_test_result_class, begin_test_suite, end_test_suite, in_teamcity

import tests.neo4j.suites as suites
import tests.stub.suites as stub_suites


# Environment variables
env_driver_name    = 'TEST_DRIVER_NAME'
env_driver_repo    = 'TEST_DRIVER_REPO'
env_testkit_branch = 'TEST_BRANCH'

containers = ["driver", "neo4jserver"]
networks = ["the-bridge"]


def cleanup():
    for c in containers:
        subprocess.run(["docker", "rm", "-f", "-v", c],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for n in networks:
        subprocess.run(["docker", "network", "rm", n],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_driver_image(root_path, branch_name, driver_name):
    """ Ensures that an up to date Docker image exists for the driver.
    """
    # Construct Docker image name from driver name (i.e drivers-go, drivers-java)
    # and branch name (i.e 4.2, go-1.14-image)
    image_name = "drivers-%s:%s" % (driver_name, branch_name)
    image_path = os.path.join(root_path, "driver", driver_name)

    # Build the image using caches, this will rebuild the image if it has changed
    # in git repo or if the image doesn't exist on this agent/host. A build will
    # occure once per driver/agent/branch (reusing layers for different branches if
    # the image file hasn't changed).
    #
    # This will use the driver folder as build context.
    print("Building driver Docker image %s from %s" % (image_name, image_path))
    subprocess.check_call(["docker", "build", "--tag", image_name, image_path])

    print("Checking for dangling intermediate images")
    images = subprocess.check_output([
        "docker", "images", "-a", "--filter=dangling=true", "-q"
    ]).splitlines()
    if len(images):
        print("Cleaning up images: %s" % images)
        subprocess.check_call(["docker", "rmi", " ".join(images)])

    return image_name


if __name__ == "__main__":
    driverName = os.environ.get(env_driver_name)
    if not driverName:
        print("Missing environment variable %s that contains name of the driver" % env_driver_name)
        sys.exit(1)

    testkitBranch = os.environ.get(env_testkit_branch)
    if not testkitBranch:
        if in_teamcity:
            print("Missing environment variable %s that contains name of testkit branch. "
                  "This name is used to name Docker repository. " % env_testkit_branch)
            sys.exit(1)
        testkitBranch = "local"

    # Retrieve path to the repository containing this script.
    # Use this path as base for locating a whole bunch of other stuff.
    thisPath = os.path.dirname(os.path.abspath(__file__))

    # Retrieve path to driver git repository
    driverRepo = os.environ.get(env_driver_repo)
    if not driverRepo:
        print("Missing environment variable %s that contains path to driver repository" % env_driver_repo)
        sys.exit(1)

    driverImage = ensure_driver_image(thisPath, testkitBranch, driverName)
    if not driverImage:
        sys.exit(1)

    # Prepare collecting of artifacts, collected to ./artifcats/now
    # Use a subfolder of current time to make it easier to run on host and not losing logs
    artifactsPath = os.path.abspath(os.path.join(".", "artifacts",datetime.now().strftime("%Y%m%d_%H%M")))
    if not os.path.exists(artifactsPath):
        os.makedirs(artifactsPath)
    print("Putting artifacts in %s" % artifactsPath)

    # Add this path to python sys path to be able to invoke modules from this repo
    os.environ['PYTHONPATH'] = thisPath

    # Important to stop all docker images upon exit
    atexit.register(cleanup)

    # Also make sure that none of those images are running at this point
    cleanup()

    # Create network to be shared among the instances
    # Retrieve the gateway (docker host) to be able to start stub server on host
    # but available to driver container.
    subprocess.run([
        "docker", "network", "create", "the-bridge"
    ])
    networkConfig = subprocess.check_output([
        "docker", "network", "inspect", "the-bridge"
    ])
    networkConfig = json.loads(networkConfig)
    gateway = networkConfig[0]['IPAM']['Config'][0]['Gateway']
    os.environ['TEST_STUB_ADDRESS'] = gateway

    # Bootstrap the driver docker image by running a bootstrap script in the image.
    # The driver docker image only contains the tools needed to build, not the built driver.
    p = subprocess.Popen([
        "docker", "run",
        # This repo monted as /nutkit
        "-v", "%s:/nutkit" % thisPath,
        # The driver repo mounted as /driver
        "-v", "%s:/driver" % driverRepo,
        # Artifacts mounted as /artifacts
        "-v", "%s:/artifacts" % artifactsPath,
        # Name of the docker container
        "--name", "driver",
        # Expose backend on this port
        "-p9876:9876",
        # Set working folder to the driver
        "-w", "/driver",
        # Remove itself upon exit
        "--rm",
        "--net=the-bridge",
        # Name of the image
        driverImage,
        # Bootstrap command
        "python3", "/nutkit/driver/bootstrap.py"
    ], bufsize=0, encoding='utf-8', stdout=subprocess.PIPE)
    print("Waiting for driver container to start")
    line = p.stdout.readline()
    if line.strip() != "ok":
        print(line)
        sys.exit(2)
    print("Started driver container")


    # Build the driver and it's nutkit backend
    print("Build driver and test backend in driver container")
    subprocess.run([
        "docker", "exec",
        "driver",
        "python3", "/nutkit/driver/%s/build.py" % driverName
    ], check=True)
    print("Finished building driver and test backend")


    """
    Unit tests
    """
    begin_test_suite('Unit tests')
    subprocess.run([
        "docker", "exec",
        "driver",
        "python3", "/nutkit/driver/%s/unittests.py" % driverName
    ], check=True)
    end_test_suite('Unit tests')

    # Start the test backend in the driver Docker instance.
    # Note that this is done detached which means that we don't know for
    # sure if the test backend actually started and we will not see
    # any output of this command.
    # When failing due to not being able to connect from client or seeing
    # issues like 'detected possible backend crash', make sure that this
    # works simply by commenting detach and see that the backend starts.
    print("Start test backend in driver container")
    subprocess.run([
        "docker", "exec",
        "--detach",
        "driver",
        "python3", "/nutkit/driver/%s/backend.py" % driverName
    ], check=True)
    print("Started test backend")
    # Wait until backend started
    time.sleep(1)

    failed = False

    """
    Stub tests, protocol version 4
    """
    suiteName = "Stub tests, protocol 4"
    begin_test_suite(suiteName)
    runner = unittest.TextTestRunner(resultclass=get_test_result_class(), verbosity=100)
    result = runner.run(stub_suites.protocol4x0)
    if result.errors or result.failures:
        failed = True
    end_test_suite(suiteName)

    if failed:
        sys.exit(1)


    """
    Neo4j 4.0 server tests
    """
    # TODO: Write suite name to TeamCity

    # Start a Neo4j server
    # Make an artifacts folder where the database can place it's logs, each time
    # we start a database server we should use a different folder.
    neo4jArtifactsPath = os.path.join(artifactsPath, "neo4j")
    os.makedirs(neo4jArtifactsPath)
    neo4jserver = "neo4jserver"
    print("Starting neo4j server")
    subprocess.run([
        "docker", "run",
        # Name of the docker container
        "--name", neo4jserver,
        # Remove itself upon exit
        "--rm",
        # Collect logs into the artifacts tree
        "-v", "%s:/logs" % os.path.join(neo4jArtifactsPath, "logs"),
        # Run in background
        "--detach",
        "--env", "NEO4J_dbms_connector_bolt_advertised__address=%s:7687" % neo4jserver,
        "--net=the-bridge",
        # Force a password
        "--env", "NEO4J_AUTH=%s/%s" % ("neo4j", "pass"),
        # Image
        "neo4j:latest",
    ])
    print("Neo4j container server started, waiting for port to be available")

    # Wait until server is listening before running tests
    # Use driver container to check for Neo4j availability since connect will be done from there
    subprocess.run([
        "docker", "exec",
        "driver",
        "python3", "/nutkit/driver/wait_for_port.py", neo4jserver, "%d" % 7687
    ], check=True)
    print("Neo4j in container listens")

    # Make sure that the tests instruct the driver to connect to neo4jserver docker container
    os.environ['TEST_NEO4J_HOST'] = neo4jserver

    suiteName = "Integration tests, Neo4j latest"
    begin_test_suite(suiteName)
    runner = unittest.TextTestRunner(resultclass=get_test_result_class(), verbosity=100)
    result = runner.run(suites.single_community_neo4j4x0)
    if result.errors or result.failures:
        failed = True
    end_test_suite(suiteName)

    if failed:
        sys.exit(1)


    """
    TODO:
    Neo4j 4.1 server tests
    """

