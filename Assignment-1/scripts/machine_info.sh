#!/bin/sh
# machine_info.sh -- describes the machine a measurement campaign ran on.
# Prints "key: value" lines (read by scripts/plot.py for the configuration table of the report and
# for the core-count markers of the figures), then the raw output of the system tools.
# Works on macOS (sysctl) and Linux (lscpu, /proc, /sys). POSIX sh, no dependency.
#
#   sh scripts/machine_info.sh > results/<host>/machine.txt      (run_bench.py does this)
#
# Environment: PC_HOST (name of the machine in the results), CXX, CXXFLAGS (override what the
# Makefile reports).

ROOT=$(cd "$(dirname "$0")/.." && pwd)
kv() { [ -n "$2" ] && printf '%s: %s\n' "$1" "$2"; return 0; }
first() { sed -n '1p'; }
human() {                       # bytes -> "64 KiB" / "16 MiB" / "24 GiB"
    awk -v b="$1" 'BEGIN { if (b == "" || b + 0 <= 0) exit;
        if (b >= 1073741824) printf "%.4g GiB", b / 1073741824;
        else if (b >= 1048576) printf "%.4g MiB", b / 1048576;
        else if (b >= 1024) printf "%.4g KiB", b / 1024;
        else printf "%d B", b }'
}

OS=$(uname -s)
HOST=${PC_HOST:-$(hostname 2>/dev/null | cut -d. -f1)}
kv host "$HOST"
kv date "$(date '+%Y-%m-%d %H:%M:%S %z')"
kv arch "$(uname -m)"
kv kernel "$(uname -sr)"

if [ "$OS" = "Darwin" ]; then
    sc() { sysctl -n "$1" 2>/dev/null; }
    kv os "macOS $(sw_vers -productVersion 2>/dev/null) ($(sw_vers -buildVersion 2>/dev/null))"
    kv cpu_model "$(sc machdep.cpu.brand_string)"
    kv hw_model "$(sc hw.model)"
    kv logical_cpus "$(sc hw.logicalcpu)"
    kv physical_cores "$(sc hw.physicalcpu)"
    NLEV=$(sc hw.nperflevels)
    if [ "${NLEV:-0}" -ge 2 ]; then
        # perflevel0 is the fastest class (performance cores), perflevel1 the efficiency cores.
        kv performance_cores "$(sc hw.perflevel0.physicalcpu)"
        kv efficiency_cores "$(sc hw.perflevel1.physicalcpu)"
        kv core_types "$(sc hw.perflevel0.physicalcpu) x $(sc hw.perflevel0.name) + $(sc hw.perflevel1.physicalcpu) x $(sc hw.perflevel1.name)"
        kv l1d_cache "$(human "$(sc hw.perflevel0.l1dcachesize)") (P) / $(human "$(sc hw.perflevel1.l1dcachesize)") (E)"
        kv l1i_cache "$(human "$(sc hw.perflevel0.l1icachesize)") (P) / $(human "$(sc hw.perflevel1.l1icachesize)") (E)"
        kv l2_cache "$(human "$(sc hw.perflevel0.l2cachesize)") per $(sc hw.perflevel0.cpusperl2) P cores / $(human "$(sc hw.perflevel1.l2cachesize)") per $(sc hw.perflevel1.cpusperl2) E cores"
    else
        kv l1d_cache "$(human "$(sc hw.l1dcachesize)")"
        kv l1i_cache "$(human "$(sc hw.l1icachesize)")"
        kv l2_cache "$(human "$(sc hw.l2cachesize)")"
    fi
    kv l3_cache "$(human "$(sc hw.l3cachesize)")"
    kv cache_line_bytes "$(sc hw.cachelinesize)"
    kv ram "$(human "$(sc hw.memsize)")"
    kv smt "$([ "$(sc hw.logicalcpu)" != "$(sc hw.physicalcpu)" ] && echo yes || echo no)"
    kv power "$(pmset -g batt 2>/dev/null | first | sed "s/.*'\(.*\)'.*/\1/")"
    kv low_power_mode "$(pmset -g 2>/dev/null | awk '/lowpowermode/ { print $2 }' | first)"
    kv thread_pinning "not available (macOS has no thread-affinity API on Apple Silicon)"
else
    lc() { LC_ALL=C lscpu 2>/dev/null | awk -F: -v k="$1" '$1 == k { sub(/^[ \t]+/, "", $2); print $2; exit }'; }
    cache() {                   # size of the cache at /sys index $1, e.g. "32K"
        cat "/sys/devices/system/cpu/cpu0/cache/index$1/size" 2>/dev/null
    }
    if [ -r /etc/os-release ]; then
        kv os "$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-$NAME}")"
    else
        kv os "$OS"
    fi
    MODEL=$(lc "Model name")
    [ -z "$MODEL" ] && MODEL=$(awk -F: '/model name|Hardware|cpu model/ { sub(/^[ \t]+/, "", $2); print $2; exit }' /proc/cpuinfo 2>/dev/null)
    kv cpu_model "$MODEL"
    LOGICAL=$(getconf _NPROCESSORS_ONLN 2>/dev/null)
    kv logical_cpus "$LOGICAL"
    SOCKETS=$(lc "Socket(s)"); CPS=$(lc "Core(s) per socket"); TPC=$(lc "Thread(s) per core")
    if [ -n "$SOCKETS" ] && [ -n "$CPS" ]; then
        kv physical_cores "$((SOCKETS * CPS))"
    fi
    kv sockets "$SOCKETS"
    kv threads_per_core "$TPC"
    kv smt "$([ "${TPC:-1}" -gt 1 ] 2>/dev/null && echo yes || echo no)"
    kv numa_nodes "$(lc "NUMA node(s)")"
    # Hybrid x86 (performance + efficiency cores) exposes two PMU devices.
    if [ -r /sys/devices/cpu_core/cpus ] && [ -r /sys/devices/cpu_atom/cpus ]; then
        kv core_types "P logical CPUs $(cat /sys/devices/cpu_core/cpus), E logical CPUs $(cat /sys/devices/cpu_atom/cpus)"
    fi
    kv l1d_cache "$(cache 0)"
    kv l1i_cache "$(cache 1)"
    kv l2_cache "$(cache 2)"
    kv l3_cache "$(cache 3)"
    CL=$(cat /sys/devices/system/cpu/cpu0/cache/index0/coherency_line_size 2>/dev/null)
    [ -z "$CL" ] && CL=$(getconf LEVEL1_DCACHE_LINESIZE 2>/dev/null)
    kv cache_line_bytes "$CL"
    kv ram "$(awk '/MemTotal/ { printf "%.4g GiB", $2 / 1048576 }' /proc/meminfo 2>/dev/null)"
    kv cpu_governor "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
    kv cpu_max_mhz "$(lc "CPU max MHz")"
    kv virtualization "$(lc "Hypervisor vendor")"
    for ps in /sys/class/power_supply/A*/online; do
        [ -r "$ps" ] && kv power "$([ "$(cat "$ps")" = 1 ] && echo 'AC Power' || echo 'Battery Power')" && break
    done
    kv thread_pinning "available (sched_setaffinity), not used by the benchmarks"
fi

# Compiler and flags: ask the Makefile (target pc-print-config in mk/zz_pack.mk), so that the
# file records what the benchmarks were really built with.
CFG=$(make -s --no-print-directory -C "$ROOT" pc-print-config 2>/dev/null)
MK_CXX=$(printf '%s\n' "$CFG" | sed -n 's/^cxx: //p')
MK_FLAGS=$(printf '%s\n' "$CFG" | sed -n 's/^cxxflags: //p')
MK_OMP=$(printf '%s\n' "$CFG" | sed -n 's/^openmp: //p')
CXX_BIN=${CXX:-${MK_CXX:-c++}}
kv compiler "$("$CXX_BIN" --version 2>/dev/null | first)"
kv compiler_command "$CXX_BIN"
kv cxxflags "${CXXFLAGS:-${MK_FLAGS:--std=c++20 -O2 -Wall -Wextra -pthread}}"
kv openmp "${MK_OMP:-unknown}"
kv cache_line_used_by_code "$(printf '#include "platform.hpp"\n#include <cstdio>\nint main(){std::printf("%%zu", pc::kCacheLine);}\n' > "${TMPDIR:-/tmp}/pc_cl_$$.cpp" 2>/dev/null \
    && "$CXX_BIN" -std=c++20 -I"$ROOT/include" "${TMPDIR:-/tmp}/pc_cl_$$.cpp" -o "${TMPDIR:-/tmp}/pc_cl_$$" 2>/dev/null \
    && "${TMPDIR:-/tmp}/pc_cl_$$"; rm -f "${TMPDIR:-/tmp}/pc_cl_$$" "${TMPDIR:-/tmp}/pc_cl_$$.cpp")"
kv python "$(python3 --version 2>&1)"
kv load_average "$(uptime | sed 's/.*load average[s]*: *//')"

echo
echo "---- raw ----"
if [ "$OS" = "Darwin" ]; then
    sysctl hw machdep.cpu 2>/dev/null | grep -v -i -E 'serial|uuid'
    echo
    pmset -g batt 2>/dev/null
else
    LC_ALL=C lscpu 2>/dev/null
    echo
    grep -E 'MemTotal|MemAvailable|Hugepagesize' /proc/meminfo 2>/dev/null
    echo
    cat /proc/version 2>/dev/null
fi
