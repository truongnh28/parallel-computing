# Build rules shared by every assignment in this folder. An assignment's
# CMakeLists.txt includes this file and calls pc_add_assignment(); it then
# builds on its own (cmake -S Assignment-1) or as part of ../CMakeLists.txt.
#
# Target names carry the assignment folder as a prefix (Assignment-1_test_locks)
# so that two assignments can both have a test_locks. The executable file keeps
# the plain name (test_locks), as in the Makefiles.

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Benchmarks are only meaningful with optimisation on, so a build without an
# explicit type is a Release build. CLion passes its own type per profile.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O2")

option(PC_TSAN   "Also build a ThreadSanitizer variant <name>_tsan of every test" ON)
option(PC_OPENMP "Compile the OpenMP baselines when the compiler supports OpenMP" ON)

find_package(Threads REQUIRED)

# Apple clang has no bundled OpenMP runtime; tell FindOpenMP where Homebrew's libomp is.
if(PC_OPENMP AND CMAKE_CXX_COMPILER_ID STREQUAL "AppleClang" AND NOT DEFINED OpenMP_CXX_FLAGS)
    foreach(prefix /opt/homebrew/opt/libomp /usr/local/opt/libomp)
        if(EXISTS ${prefix}/include/omp.h)
            set(OpenMP_CXX_FLAGS "-Xclang -fopenmp -I${prefix}/include")
            set(OpenMP_CXX_LIB_NAMES omp)
            set(OpenMP_omp_LIBRARY ${prefix}/lib/libomp.dylib)
            break()
        endif()
    endforeach()
endif()
if(PC_OPENMP)
    find_package(OpenMP COMPONENTS CXX QUIET)
endif()

enable_testing()

# pc_add_program(<name> <source> [EXCLUDE_FROM_ALL] [TSAN] [TEST] [TEST_ARGS ...]
#                [DEFINITIONS ...])
# One executable of the current assignment. TEST registers it with ctest.
function(pc_add_program name src)
    cmake_parse_arguments(arg "EXCLUDE_FROM_ALL;TSAN;TEST" "" "TEST_ARGS;DEFINITIONS" ${ARGN})
    set(target ${PC_PREFIX}_${name})
    set(exclude)
    if(arg_EXCLUDE_FROM_ALL)
        set(exclude EXCLUDE_FROM_ALL)
    endif()
    add_executable(${target} ${exclude} ${src})
    set_target_properties(${target} PROPERTIES OUTPUT_NAME ${name})
    target_link_libraries(${target} PRIVATE ${PC_PREFIX}_common)
    if(arg_DEFINITIONS)
        target_compile_definitions(${target} PRIVATE ${arg_DEFINITIONS})
    endif()
    if(arg_TSAN)
        target_compile_options(${target} PRIVATE -O1 -g -fsanitize=thread)
        target_link_options(${target} PRIVATE -fsanitize=thread)
    endif()
    if(arg_TEST)
        add_test(NAME ${PC_PREFIX}/${name} COMMAND ${target} ${arg_TEST_ARGS})
    endif()
endfunction()

# pc_add_assignment()
# The standard programs of the assignment in the current directory: every
# tests/*.cpp and bench/*.cpp is a standalone program with its own main().
function(pc_add_assignment)
    get_filename_component(prefix ${CMAKE_CURRENT_SOURCE_DIR} NAME)
    set(PC_PREFIX ${prefix})
    set(PC_PREFIX ${prefix} PARENT_SCOPE)

    # Settings shared by every program. As in the Makefiles, OpenMP goes into
    # every program when available: the OpenMP baselines sit under #ifdef _OPENMP.
    add_library(${prefix}_common INTERFACE)
    target_include_directories(${prefix}_common INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_compile_options(${prefix}_common INTERFACE -Wall -Wextra)
    target_link_libraries(${prefix}_common INTERFACE Threads::Threads)
    if(OpenMP_CXX_FOUND)
        target_link_libraries(${prefix}_common INTERFACE OpenMP::OpenMP_CXX)
    endif()

    # A header gets compile flags in CLion only when some target lists it, so
    # every header in include/ goes into this target. Building it compiles each
    # header on its own, which also checks that the header includes everything
    # it uses. It is not part of the default build.
    file(GLOB headers CONFIGURE_DEPENDS
            ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h
            ${CMAKE_CURRENT_SOURCE_DIR}/include/*.hpp)
    set(check_sources)
    foreach(hdr ${headers})
        get_filename_component(hdr_name ${hdr} NAME)
        set(check_src ${CMAKE_CURRENT_BINARY_DIR}/header_check/${hdr_name}.cpp)
        file(CONFIGURE OUTPUT ${check_src} CONTENT "#include \"${hdr_name}\"\n" @ONLY)
        list(APPEND check_sources ${check_src})
    endforeach()
    add_library(${prefix}_header_check OBJECT EXCLUDE_FROM_ALL ${check_sources} ${headers})
    target_link_libraries(${prefix}_header_check PRIVATE ${prefix}_common)

    # CONFIGURE_DEPENDS picks up new files on the next build.
    file(GLOB test_sources  CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/tests/*.cpp)
    file(GLOB bench_sources CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/bench/*.cpp)

    foreach(src ${test_sources})
        get_filename_component(name ${src} NAME_WE)
        pc_add_program(${name} ${src} TEST)
        # TSan slows the program down 5-15x; "small" shrinks the sizes (make tsan).
        if(PC_TSAN)
            pc_add_program(${name}_tsan ${src} TSAN TEST TEST_ARGS small)
        endif()
    endforeach()

    foreach(src ${bench_sources})
        get_filename_component(name ${src} NAME_WE)
        pc_add_program(${name} ${src})
    endforeach()
endfunction()
