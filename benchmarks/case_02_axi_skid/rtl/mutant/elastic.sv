// case_02_axi_skid, mutant variant. Same external contract as the correct
// variant. Never compile the two variants together.
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
      end else begin
        full_q <= 1'b0;
      end
    end
  end
endmodule
