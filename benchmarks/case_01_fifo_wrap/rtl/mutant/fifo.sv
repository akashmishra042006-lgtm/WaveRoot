// case_01_fifo_wrap, mutant variant. Same external contract as the correct
// variant. Never compile the two variants together.
`timescale 1ns/1ps
module fifo (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       in_valid,
  output logic       in_ready,
  input  logic [7:0] in_data,
  output logic       out_valid,
  input  logic       out_ready,
  output logic [7:0] out_data,
  output logic       full,
  output logic       empty
);
  logic [2:0] wptr;
  logic [2:0] rptr;
  logic [7:0] mem [4];
  logic       push;
  logic       pop;

  assign empty     = (wptr == rptr);
  assign full      = (wptr[1:0] == rptr[1:0]) && (wptr[2] != rptr[2]);
  assign out_valid = !empty;
  assign out_data  = mem[rptr[1:0]];
  assign pop       = out_valid && out_ready;
  assign in_ready  = !full || pop;
  assign push      = in_valid && in_ready;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      wptr <= 3'b000;
      rptr <= 3'b000;
    end else begin
      if (push) begin
        mem[wptr[1:0]] <= in_data;
        if (wptr[1:0] == 2'b11)
          wptr <= {wptr[2], 2'b00};
        else
          wptr <= wptr + 3'd1;
      end
      if (pop) begin
        if (rptr[1:0] == 2'b11)
          rptr <= {~rptr[2], 2'b00};
        else
          rptr <= rptr + 3'd1;
      end
    end
  end
endmodule
