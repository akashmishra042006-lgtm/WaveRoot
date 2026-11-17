`timescale 1ns/1ps
// Testbench matching spec.md section 16: clk rises at 5 ns and every 10 ns,
// rst_n low until 20 ns, writes 0x11-0x55 from 20 ns to 70 ns, out_ready from
// 60 ns; finish at 100 ns. Inputs equal tests/fixtures/mock_fifo_failure.json.
//
// Regenerates the real simulator fixtures next to it (run from repo root):
//   $ iverilog -g2012 -DDUMPFILE='"tests/fixtures/waveform/mock_fifo_icarus.vcd"' -o tb.vvp \
//       tests/fixtures/waveform/mock_fifo_tb.sv tests/fixtures/mock_fifo_wrap.sv && vvp -n tb.vvp
//   $ verilator --binary --timing --trace -DDUMPFILE='"tests/fixtures/waveform/mock_fifo_verilator.vcd"' \
//       --top-module tb tests/fixtures/waveform/mock_fifo_tb.sv tests/fixtures/mock_fifo_wrap.sv && ./obj_dir/Vtb
// Produced with Icarus Verilog 13.0 and Verilator 5.054.
module tb;
  logic clk = 1'b0;
  logic rst_n = 1'b0;
  logic in_valid = 1'b0;
  logic in_ready;
  logic [7:0] in_data = 8'h00;
  logic out_valid;
  logic out_ready = 1'b0;
  logic [7:0] out_data;
  logic full;
  logic empty;

  mock_fifo_wrap dut (.*);

  initial forever #5 clk = ~clk;  // rises at 5, 15, 25, ... ns

  initial begin
    $dumpfile(`DUMPFILE);
    $dumpvars(0, tb);
    #20 rst_n = 1'b1; in_valid = 1'b1; in_data = 8'h11;
    #10 in_data = 8'h22;
    #10 in_data = 8'h33;
    #10 in_data = 8'h44;
    #10 in_data = 8'h55; out_ready = 1'b1;
    #10 in_valid = 1'b0;
    #30 $finish;
  end
endmodule
