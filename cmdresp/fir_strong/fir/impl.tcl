# impl.tcl -- Vivado place and route of an already-synthesized csynth project, for
# post-route timing and resources (the HLS estimate is only an estimate).
#
#   WAVEFLOW_FIR_BW=32 vitis-run --mode hls --tcl impl.tcl      (needs prj_csynth_w32)
#
# Report: prj_csynth_w<bw>/solution1/impl/report/verilog/<top>_export.rpt

set bw $::env(WAVEFLOW_FIR_BW)
open_project prj_csynth_w${bw}
open_solution "solution1"
if {[catch {export_design -flow impl -rtl verilog -format ip_catalog} res]} {
    puts "WAVEFLOW_ERROR: implementation failed."
    puts $res
    exit 1
}
puts "WAVEFLOW_SUCCESS: implementation done."
exit 0
