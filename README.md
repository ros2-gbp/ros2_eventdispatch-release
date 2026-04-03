# initial release notes

bloom toolchain:

```
sudo apt install python3-bloom python3-rosdep fakeroot dh-make

#####################################

multipass jazzy

https://robotics.stackexchange.com/a/84789
this will let
    fakeroot  debian/rules binary
find local rosdep dependencies

    1. dpkg install ros deb files

    2. sudo vim /etc/ros/rosdep/sources.list.d/20-default.list 

        yaml file:///tmp/test_ws/src/ros2_eventdispatch-release/local.yaml

            eventdispatch_ros2_interfaces:
              ubuntu: [ros-jazzy-eventdispatch-ros2-interfaces]

            eventdispatch_python:
              ubuntu: [ros-jazzy-eventdispatch-python]

    3. rosdep update (see the packages)

    4. fakeroot / bloom build the remaining packages

--- make all 3 deb files --- using bloom
```

https://docs.ros.org/en/jazzy/How-To-Guides/Releasing/First-Time-Release.html

* [PR 907](https://github.com/ros2-gbp/ros2-gbp-github-org/issues/907)
* [PR 908](https://github.com/ros2-gbp/ros2-gbp-github-org/issues/908)

---

https://robotics.stackexchange.com/a/117904/53773

[upstream](https://github.com/cyan-at/ros2_eventdispatch.git)

[release](https://github.com/ros2-gbp/ros2_eventdispatch-release.git)

---

```
bloom-release --new-track --rosdistro jazzy --track jazzy ros2_eventdispatch
```

for 'amending' a tag:
```
git tag 0.2.25 2c4bf0ca5263accd5a661051994e0913de361621 -f
git push origin refs/tags/0.2.25 --force
```

---

# update

repeat until functionally tested:
1. update source
2. colcon ws build, test, func validate

