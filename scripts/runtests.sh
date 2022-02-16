#!/usr/bin/env bash
##
## run tests in multiple environments
##    @script.name [option]
##
## This runs the project tests inside multiple docker images and collects
## results.
##
## Options:
##    --specs=VALUE   specs file, defaults to ./docker/test_images.txt
##    --image=VALUE   the image to run tests for
##    --tests=VALUE   the tests to run as package.module
##    --extras=VALUE  extras to install before running
##    --label=VALUE   the label for this test
##    --clean         clean testlogs before starting
##    --shell         enter shell after running tests
##
##    Specifying --specs overrides --image, --tests, --extras, --labels.
##    Specifying no option, or --specs, implies --clean.
##
## How it works:
##
## For each image listed in specs file (test_images.txt),
##
## 1. run the docker container, downloading the image if not cached yet
## 2. install the project (pip install -e)
## 3. install any additional dependencies, if listed for the image
## 4. run the tests
## 5. freeze pip packages (for reproducing and reference)
## 6. collect test results (creates a tgz for each run)
##
## Finally, print a test summary and exist with non-zero if any one of the
## tests failed.
##
# script setup to parse options
script_dir=$(dirname "$0")
script_dir=$(realpath $script_dir)
source $script_dir/easyoptions || exit

# location of project source under test (matches in-container $test_base)
sources_dir=$script_dir/..
# all images we want to test, and list of tests
test_images=${specs:-$script_dir/docker/test_images.txt}
# in-container location of project source under test
test_base=/var/project
# host log files
test_logbase=/tmp/testlogs
# host test rc file
test_logrc=$test_logbase/tests_rc.log

function runimage() {
  # args
  tests=$2
  extras=$3
  pipreq=$4
  pipopts=$5
  label=$6
  # run
  test_label=${label:-${tests//[^[:alnum:]]/_}}
  echo "INFO runtests: running $tests on $image"
  # host name of log directory for this test
  test_logdir=$test_logbase/$(dirname $image)/$(basename $image)/$test_label
  # host name of log file
  test_logfn=$test_logdir/$(basename $image).log
  # host name of pip freeze output file
  test_pipfn=$test_logdir/pip-requirements.lst
  # host name of final results tar
  test_logtar=$test_logbase/$(dirname $image)_$(basename $image)_$test_label.tgz
  # start test container
  mkdir -p $test_logdir
  extras=${extras:-dev}
  pipreq=${pipreq:-pip}
  docker rm -f omegaml-test
  echo "INFO runtests pulling docker images (quiet)"
  docker pull -q $image
  echo "INFO runtests starting tests now"
  # docker run arguments
  # --network specifies to use the host network so we can access mongodb, rabbitmq
  # --name name of container, useful for further docker commands
  # --user, --group-add-users specify the jupyter stacks user
  # -dt deamon with tty
  # -v maps the host path to the container
  # -w container working directory
  # jupyter stacks options
  # -- see https://jupyter-docker-stacks.readthedocs.io/en/latest/using/common.html
  #    GRANT_SUDO, allow use of sudo e.g. for apt
  # Makefile options
  # TESTS, EXTRAS, PIPREQ see Makefile:install
  docker run --network host \
             --name omegaml-test \
             --user $UID --group-add users \
             -dt \
             -e GRANT_SUDO=yes \
             -e TESTS="$tests" \
             -e EXTRAS="dev,$extras" \
             -e PIPREQ="$pipreq" \
             -e PIPOPTS="$pipopts" \
             -v $sources_dir:$test_base \
             -w $test_base $image \
             bash
  # run commands, collect results, cleanup
  # -- some images don't have make installed, e.g. https://github.com/jupyter/docker-stacks/issues/1625
  docker exec -e GRANT_SUDO=yes --user root omegaml-test bash -c "which make || apt update && apt -y install build-essential"
  docker exec omegaml-test bash -c 'make install test; echo $? > /tmp/test.status' 2>&1 | tee -a $test_logfn
  docker exec omegaml-test bash -c "cat /tmp/test.status" | xargs -I RC echo "$test_logdir==RC" >> $test_logrc
  docker exec omegaml-test bash -c "pip list --format freeze" | tee -a ${test_pipfn}
  tar -czf $test_logtar $test_logdir --remove-files
  if [[ ! -z $shell ]]; then
    docker exec -it omegaml-test bash
  fi
  docker kill omegaml-test
  echo "INFO runtests tests completed."
}

function runauto() {
  # run tests from image specs
  # images to test against
  while IFS=';' read -r image tests extras pipreq pipopts label; do
    runimage "$image" "$tests" "$extras" "$pipreq" "$pipopts" "$label"
    docker rmi --force $image
  done < <(cat $test_images | grep -v "#")
}

function clean() {
  # start clean
  rm -rf $sources_dir/build
  rm -rf $sources_dir/dist
  rm -rf $test_logbase
  mkdir -p $test_logbase
}

function summary() {
  # print summary
  echo "All Tests Summary (==return code)"
  echo "================="
  cat $test_logrc
  echo "-----------------"
  # man grep: exit status is 0 if a line is selected, 1 if no lines were selected
  # -- if at least one line does not have ==0 => grep rc 0 => return rc 1
  rc=$([[ ! $(grep -v "==0" $test_logrc) ]])
  exit $rc
}

function main() {
  if [ ! -z $clean ]; then
    clean
  fi
  if [ ! -z $image ]; then
    runimage "$image" "$tests" "$extras" "$pipreq" "$pipopts" "$label"
    summary
  else
    clean
    runauto
    summary
  fi
}

main
