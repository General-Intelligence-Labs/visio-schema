cmake_minimum_required(VERSION 3.20)
project(visio_serial_consumer CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Pull in the one wholistic target (generated bindings + framing codec).
# Its own tests stay off in this nested build.
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../../cpp ${CMAKE_BINARY_DIR}/visio_schema)

add_executable(serial_consumer serial_consumer.cc)
target_link_libraries(serial_consumer PRIVATE visio_schema)
