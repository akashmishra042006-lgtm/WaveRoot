// case_02_axi_skid, correct variant.
// One-entry, non-fall-through elastic ready/valid buffer (historical name
// "axi_skid"; it claims only this elastic contract, not AXI compliance).
// s_ready = !full || m_ready; m_valid = full. When s_ready, the entry updates
// from s_valid (capturing s_data when valid); otherwise it holds. A stalled
// transaction keeps both valid and payload until accepted.
`timescale 1ns/1ps
module elastic (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       s_valid,
  output logic       s_ready,
  input  logic [7:0] s_data,
  output logic       m_valid,
  input  logic       m_ready,
  output logic [7:0] m_data
);
  logic       full_q;
  logic [7:0] data_q;

  assign s_ready = !full_q || m_ready;
  assign m_valid = full_q;
  assign m_data  = data_q;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      full_q <= 1'b0;
      data_q <= 8'h00;
    end else begin
      if (s_ready) begin
        full_q <= s_valid;
        if (s_valid)
          data_q <= s_data;
      end
    end
  end
endmodule
