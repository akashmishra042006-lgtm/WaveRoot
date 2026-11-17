// case_04_alu_xprop, correct variant.
// 8-bit two-operand mux with a configurable registered selector. Reset sets
// the selector to operand A and clears the output registers. The mux is a
// conditional expression (not a procedural if) so a four-state simulator
// merges per bit: with an unknown selector, bits where a and b agree still
// resolve. When configuration and input coincide, the input uses the old
// selector (registered-state semantics).
`timescale 1ns/1ps
module alu (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       in_valid,
  input  logic [7:0] a,
  input  logic [7:0] b,
  input  logic       cfg_we,
  input  logic       cfg_sel,
  output logic       out_valid,
  output logic [7:0] out_data
);
  logic sel_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      sel_q     <= 1'b0;
      out_valid <= 1'b0;
      out_data  <= 8'h00;
    end else begin
      if (cfg_we)
        sel_q <= cfg_sel;
      out_valid <= in_valid;
      if (in_valid)
        out_data <= sel_q ? b : a;
    end
  end
endmodule
