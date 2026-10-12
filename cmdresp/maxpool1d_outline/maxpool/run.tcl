# run.tcl -- Vitis HLS for the streaming 1-D max-pool accelerator.
#
#   WAVEFLOW_POLY_STAGE=csim   every scenario, C simulation
#   WAVEFLOW_POLY_STAGE=synth  C synthesis + ONE RTL co-simulation that walks through every
#                              scenario in data/w32/scenarios_cosim.txt
#
#   vitis-run --mode hls --tcl run.tcl
#
# (The stage is an environment variable because vitis-run 2025.1 has no --tclargs.)
#
# One project, one directory deep (w32_proj/): Vitis 2025.1 drops the kernel from csim when
# a project is nested deeper.  The kernel boundary (gen/maxpool.cpp, gen/maxpool.hpp) is
# generated; the kernel body (maxpool_body_impl.tpp) and the testbench (maxpool_tb.cpp) are
# hand-written.  The testbench writes each scenario's response to data/w32/<scenario>/<stage>/.
#
# Environment: WAVEFLOW_POLY_CLK_PERIOD_NS (default 10).

set script_dir [file dirname [file normalize [info script]]]
set stage ""
if {[info exists ::env(WAVEFLOW_POLY_STAGE)]} {
    set stage $::env(WAVEFLOW_POLY_STAGE)
}
if {$stage ni {csim synth}} {
    puts "WAVEFLOW_ERROR: set WAVEFLOW_POLY_STAGE to csim or synth (got '$stage')."
    exit 1
}
set top maxpool
set proj w32_proj

open_project -reset $proj
set_top $top
add_files gen/maxpool.cpp -cflags "-I."
add_files -tb maxpool_tb.cpp -cflags "-I."
set streamutils_cpp [file join $script_dir "include" "streamutils.cpp"]
if {[file exists $streamutils_cpp]} {
    add_files -tb $streamutils_cpp
}

open_solution -reset "solution1"
set_part {xc7z020clg484-1}
set clk_period_ns 10
if {[info exists ::env(WAVEFLOW_POLY_CLK_PERIOD_NS)]} {
    set clk_period_ns $::env(WAVEFLOW_POLY_CLK_PERIOD_NS)
}
create_clock -period $clk_period_ns
set data_dir [file join $script_dir "data" "w32"]

if {$stage eq "csim"} {
    if {[catch {csim_design -argv "$data_dir csim"} res]} {
        puts "WAVEFLOW_ERROR: $top C simulation failed."
        puts $res
        exit 1
    }
    puts "WAVEFLOW_SUCCESS: $top C simulation passed."
    exit 0
}

if {[catch {csynth_design} res]} {
    puts "WAVEFLOW_ERROR: $top C synthesis failed."
    puts $res
    exit 1
}
if {[catch {cosim_design -argv "$data_dir cosim" -trace_level none} res]} {
    puts "WAVEFLOW_ERROR: $top RTL co-simulation failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: $top C synthesis and RTL co-simulation passed."
exit 0
