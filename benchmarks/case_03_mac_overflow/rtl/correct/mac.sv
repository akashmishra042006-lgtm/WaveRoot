// case_03_mac_overflow, correct variant.
// Signed int8 product into a signed 16-bit saturating accumulator with one
// registered output stage. The product and accumulator are widened to 17 bits
// before the addition and the overflow comparison; clamping happens on the
// wide sum. The output registers the pre-edge accumulator, and out_valid
// registers the previous cycle's in_valid, giving one extra stage of latency.
`timescale 1ns/1ps
module mac (
  input  logic               clk,
  input  logic               rst_n,
  input  logic               in_valid,
  input  logic signed  [7:0] a,
  input  logic signed  [7:0] b,
  output logic               out_valid,
  output logic signed [15:0] out_data
);
  logic signed [15:0] acc_q;
  logic               valid_q;
  logic signed [15:0] prod;
  logic signed [16:0] sum_wide;
  logic signed [16:0] sum_cmp;
  logic signed [15:0] next_acc;

  assign prod     = 16'(a * b);
  assign sum_wide = 17'(acc_q) + 17'(prod);
  assign sum_cmp  = sum_wide;
  assign next_acc = (sum_cmp > 17'sd32767)  ? 16'sd32767  :
                    (sum_cmp < -17'sd32768) ? -16'sd32768 :
                    sum_wide[15:0];

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      acc_q     <= 16'sd0;
      valid_q   <= 1'b0;
      out_valid <= 1'b0;
      out_data  <= 16'sd0;
    end else begin
      if (in_valid)
        acc_q <= next_acc;
      valid_q   <= in_valid;
      out_valid <= valid_q;
      out_data  <= acc_q;
    end
  end
endmodule
