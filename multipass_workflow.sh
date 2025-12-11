rm -rf /tmp/bloom_ws/
mkdir -p /tmp/bloom_ws/
cd /tmp/bloom_ws/

rsync -azv --exclude 'debs/' --exclude='*.deb' /home/charlieyan1/Dev/jim/ros2_eventdispatch-release .

WS_DIR=$(realpath .)
echo "WS_DIR="$WS_DIR

cd $WS_DIR/ros2_eventdispatch-release/eventdispatch_python
./bloom_toolchain.sh

cd $WS_DIR/ros2_eventdispatch-release/eventdispatch_ros2_interfaces
./bloom_toolchain.sh

# multipass jazzy

# https://robotics.stackexchange.com/a/84789
# this will let
#     fakeroot  debian/rules binary
# find local rosdep dependencies

#     1. dpkg install ros deb files

#     2. sudo vim /etc/ros/rosdep/sources.list.d/20-default.list 

#         yaml file:///tmp/test_ws/src/ros2_eventdispatch-release/local.yaml

#             eventdispatch_ros2_interfaces:
#               ubuntu: [ros-jazzy-eventdispatch-ros2-interfaces]

#             eventdispatch_python:
#               ubuntu: [ros-jazzy-eventdispatch-python]

#     3. rosdep update (see the packages)

#     4. fakeroot / bloom build the remaining packages

# --- make all 3 deb files --- using bloom

cd $WS_DIR/ros2_eventdispatch-release/eventdispatch_ros2
./bloom_toolchain.sh

cd $WS_DIR/ros2_eventdispatch-release

echo "####################### debs live"
echo $(realpath .)
ls -l *.deb