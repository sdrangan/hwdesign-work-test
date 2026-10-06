# run.tcl -- Vitis HLS for the command-driven FIR accelerator.
#
#   WAVEFLOW_FIR_STAGE=csim   C simulation of every scenario
#   WAVEFLOW_FIR_STAGE=synth  C synthesis + RTL co-simulation of every scenario
#   WAVEFLOW_FIR_STAGE=csynth C synthesis only (quick timing/resource iterations)
#
# with WAVEFLOW_FIR_BW (32 | 64) and WAVEFLOW_FIR_TOP (fir | fir_bw64).  (The stage is an
# environment variable because vitis-run 2025.1 has no --tclargs.)  Each run is its own
# one-level project, prj_<stage>_w<bw>, with absolute paths to every file.
#
# The kernel boundary (gen/fir.cpp, gen/fir.hpp) is generated; the kernel body
# (fir_body_impl.tpp, included from gen/fir.hpp) and the testbench (fir_tb.cpp) are
# hand-written.  The testbench writes each scenario's response to data/w<bw>/<scenario>/<stage>/.
#
# Environment: WAVEFLOW_FIR_CLK_PERIOD_NS (default 10), WAVEFLOW_FIR_TRACE_LEVEL
# (none | port | all, default none; port records the waveform the timing figures use).

set script_dir [file dirname [file normalize [info script]]]
foreach {var name} {stage WAVEFLOW_FIR_STAGE bw WAVEFLOW_FIR_BW top WAVEFLOW_FIR_TOP} {
    if {![info exists ::env($name)]} {
        puts "WAVEFLOW_ERROR: set $name."
        exit 1
    }
    set $var $::env($name)
}
if {$stage ni {csim synth csynth}} {
    puts "WAVEFLOW_ERROR: set WAVEFLOW_FIR_STAGE to csim, synth or csynth (got '$stage')."
    exit 1
}

open_project -reset prj_${stage}_w${bw}
set_top $top
add_files [file join $script_dir gen fir.cpp] -cflags "-I$script_dir"
add_files -tb [file join $script_dir fir_tb.cpp] -cflags "-I$script_dir -DWORD_BW=$bw"
set streamutils_cpp [file join $script_dir "include" "streamutils.cpp"]
if {[file exists $streamutils_cpp]} {
    add_files -tb $streamutils_cpp -cflags "-I$script_dir"
}

open_solution -reset "solution1"
set_part {xc7z020clg400-1}
set clk_period_ns 10
if {[info exists ::env(WAVEFLOW_FIR_CLK_PERIOD_NS)]} {
    set clk_period_ns $::env(WAVEFLOW_FIR_CLK_PERIOD_NS)
}
create_clock -period $clk_period_ns
set trace_level "none"
if {[info exists ::env(WAVEFLOW_FIR_TRACE_LEVEL)]} {
    set trace_level $::env(WAVEFLOW_FIR_TRACE_LEVEL)
}
set data_dir [file join $script_dir "data" "w$bw"]

if {$stage eq "csim"} {
    if {[catch {csim_design -argv "$data_dir csim"} res]} {
        puts "WAVEFLOW_ERROR: $top C simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: $top C simulation passed."
} elseif {$stage eq "csynth"} {
    # Synthesis only: a quick look at timing and resources while iterating.
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: $top C synthesis failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: $top C synthesis passed."
} else {
    if {[catch {csynth_design} res]} {
        puts "WAVEFLOW_ERROR: $top C synthesis failed."
        puts $res
        exit 1
    }
    if {[catch {cosim_design -argv "$data_dir cosim" -trace_level $trace_level} res]} {
        puts "WAVEFLOW_ERROR: $top RTL co-simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: $top C synthesis and RTL co-simulation passed."
}
exit 0
