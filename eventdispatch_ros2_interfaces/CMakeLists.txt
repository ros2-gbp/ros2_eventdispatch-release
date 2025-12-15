cmake_minimum_required(VERSION 3.8)
project(eventdispatch_ros2_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/ROSEvent.srv"
  "srv/ROSEvents.srv"
  "msg/ROSEvent.msg"
  "msg/ROSEvents.msg"
  DEPENDENCIES builtin_interfaces
)

ament_package()
