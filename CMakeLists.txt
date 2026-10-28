# Every assignment in this folder, in one CMake project (for CLion and ctest).
# Each subfolder with a CMakeLists.txt is added; the shared rules are in
# cmake/PcAssignment.cmake.
cmake_minimum_required(VERSION 3.20)
project(Assignments LANGUAGES CXX)

enable_testing()

file(GLOB children LIST_DIRECTORIES true RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/*)
foreach(child ${children})
    if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/${child}/CMakeLists.txt)
        add_subdirectory(${child})
    endif()
endforeach()
