// case_01_fifo_wrap shared harness. Compiled once per variant with an explicit
// file list; the variant supplies module `fifo`.
//
// Timing: clock rises at 5000 ps and every 10000 ps after. Reset is held low
// from time 0 and the stimulus file's first line is applied at the falling
// edge at 20000 ps, so the rising edges at 5000 and 15000 ps sample reset
// active and the edge at 25000 ps is epoch 0 cycle 0 when line 0 releases
// reset. Inputs are driven on falling edges only.
//
// Stimulus line: `rst_n in_valid in_data_hex out_ready` (whitespace separated,
// `#` comments). The testbench plays every line, then finishes; the drain
// window is part of the stimulus file so the golden model sees it too.
//
// Observed JSONL: for every rising clock edge, `pre` holds values read in the
// active region at the edge (the `before` phase) and `settled` holds values
// read 1 ps later (the `settled` phase). With inputs stable around the edge,
// these equal the spec's phase queries at the edge time.
`timescale 1ps/1ps
module tb;
  localparam int unsigned HALF_PERIOD = 5000;

  logic       clk;
  logic       rst_n;
  logic       in_valid;
  logic       in_ready;
  logic [7:0] in_data;
  logic       out_valid;
  logic       out_ready;
  logic [7:0] out_data;
  logic       full;
  logic       empty;

  fifo dut (
    .clk(clk), .rst_n(rst_n),
    .in_valid(in_valid), .in_ready(in_ready), .in_data(in_data),
    .out_valid(out_valid), .out_ready(out_ready), .out_data(out_data),
    .full(full), .empty(empty)
  );

  initial begin
    clk = 1'b0;
    forever #(HALF_PERIOD) clk = ~clk;
  end

  string stim_path;
  string obs_path;
  string vcd_path;
  string line;
  integer stim_fd;
  integer obs_fd;
  integer code;
  int unsigned cycle_limit;
  int unsigned applied;
  logic [31:0] f_rst, f_iv, f_ord;
  logic [31:0] f_id;

  // Reads the next data line into f_*; skips blank and `#` comment lines.
  // Returns 1 on success, 0 at end of file or on a malformed line.
  function automatic bit next_stim_line();
    forever begin
      code = $fgets(line, stim_fd);
      if (code <= 0) return 0;
      code = $sscanf(line, " %b %b %h %b", f_rst, f_iv, f_id, f_ord);
      if (code == 4) return 1;
      if (code > 0) begin
        $fatal(1, "TB_ERROR: malformed stimulus line: %s", line);
        return 0;
      end
      // comment or blank line: keep reading
    end
    return 0;
  endfunction

  initial begin
    cycle_limit = 64;
    applied = 0;
    if (!$value$plusargs("stim=%s", stim_path)) begin
      $fatal(1, "TB_ERROR: missing +stim"); $finish;
    end
    if (!$value$plusargs("obs=%s", obs_path)) begin
      $fatal(1, "TB_ERROR: missing +obs"); $finish;
    end
    if (!$value$plusargs("vcd=%s", vcd_path)) begin
      $fatal(1, "TB_ERROR: missing +vcd"); $finish;
    end
    void'($value$plusargs("cycle_limit=%d", cycle_limit));
    stim_fd = $fopen(stim_path, "r");
    obs_fd  = $fopen(obs_path, "w");
    if (stim_fd == 0 || obs_fd == 0) begin
      $fatal(1, "TB_ERROR: cannot open files"); $finish;
    end
    $dumpfile(vcd_path);
    $dumpvars(0, tb);
    $fwrite(obs_fd, "{\"type\":\"observed_metadata\",\"case\":\"case_01_fifo_wrap\",\"time_unit_ps\":1}\n");

    rst_n = 1'b0; in_valid = 1'b0; in_data = '0; out_ready = 1'b0;
    @(negedge clk);  // 10000 ps: reset stays asserted for its second edge
    forever begin
      @(negedge clk);  // 20000 ps, then every 10000 ps
      if (applied >= cycle_limit) break;
      if (!next_stim_line()) break;
      // Blocking assigns at the falling edge: inputs settle half a period
      // before the sampling rising edge, so there is no race with sampling.
      rst_n = f_rst[0]; in_valid = f_iv[0]; in_data = f_id[7:0]; out_ready = f_ord[0];
      applied += 1;
    end
    #2;  // past the last sampling edge's settled capture
    $fclose(stim_fd);
    $fclose(obs_fd);
    $finish;
  end

  longint t_edge;
  logic p_rst, p_iv, p_ir, p_or, p_full, p_empty, p_ov;
  logic [7:0] p_id, p_od;
  always @(posedge clk) begin
    p_rst = rst_n; p_iv = in_valid; p_id = in_data; p_or = out_ready;
    p_ir = in_ready; p_full = full; p_empty = empty; p_ov = out_valid; p_od = out_data;
    t_edge = $time;
    #1;
    $fwrite(obs_fd,
      "{\"type\":\"edge\",\"time_ps\":%0d,\"pre\":{\"rst_n\":\"%b\",\"in_valid\":\"%b\",\"in_data\":\"%b\",\"out_ready\":\"%b\",\"in_ready\":\"%b\",\"full\":\"%b\",\"empty\":\"%b\",\"out_valid\":\"%b\",\"out_data\":\"%b\"},\"settled\":{\"full\":\"%b\",\"empty\":\"%b\",\"out_valid\":\"%b\",\"out_data\":\"%b\",\"in_ready\":\"%b\"}}\n",
      t_edge, p_rst, p_iv, p_id, p_or, p_ir, p_full, p_empty, p_ov, p_od,
      full, empty, out_valid, out_data, in_ready);
  end
endmodule
