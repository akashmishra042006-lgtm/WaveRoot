// case_04_alu_xprop shared harness. Same conventions as case_01's tb.sv.
// This case requires a genuine four-state simulator (Icarus Verilog in
// SystemVerilog mode); the manifest pins it. %b formatting preserves x/z in
// the observed JSONL, and the VCD must actually contain unknown bits.
// Stimulus line: `rst_n in_valid a_hex b_hex cfg_we cfg_sel`.
`timescale 1ps/1ps
module tb;
  localparam int unsigned HALF_PERIOD = 5000;

  logic       clk;
  logic       rst_n;
  logic       in_valid;
  logic [7:0] a;
  logic [7:0] b;
  logic       cfg_we;
  logic       cfg_sel;
  logic       out_valid;
  logic [7:0] out_data;

  alu dut (
    .clk(clk), .rst_n(rst_n),
    .in_valid(in_valid), .a(a), .b(b),
    .cfg_we(cfg_we), .cfg_sel(cfg_sel),
    .out_valid(out_valid), .out_data(out_data)
  );

  initial begin
    clk = 1'b0;
    forever #(HALF_PERIOD) clk = ~clk;
  end

  string stim_path;
  string obs_path;
  string vcd_path;
  reg [8*256-1:0] line;  // Icarus's $fgets requires a reg buffer, not a string
  integer stim_fd;
  integer obs_fd;
  integer code;
  int unsigned cycle_limit;
  int unsigned applied;
  bit stim_done;
  logic [31:0] f_rst, f_iv, f_we, f_sel;
  logic [31:0] f_a, f_b;

  function automatic bit next_stim_line();
    bit result;
    bit scanning;
    result = 1'b0;
    scanning = 1'b1;
    while (scanning) begin
      code = $fgets(line, stim_fd);
      if (code <= 0)
        scanning = 1'b0;  // end of file
      else begin
        code = $sscanf(line, " %b %b %h %h %b %b", f_rst, f_iv, f_a, f_b, f_we, f_sel);
        if (code == 6) begin
          result = 1'b1;
          scanning = 1'b0;
        end else if (code > 0) begin
          $display("TB_ERROR: malformed stimulus line: %0s", line);
          scanning = 1'b0;
        end
        // else: comment or blank line, keep reading
      end
    end
    return result;
  endfunction

  initial begin
    cycle_limit = 64;
    applied = 0;
    if (!$value$plusargs("stim=%s", stim_path)) begin
      $display("TB_ERROR: missing +stim"); $finish;
    end
    if (!$value$plusargs("obs=%s", obs_path)) begin
      $display("TB_ERROR: missing +obs"); $finish;
    end
    if (!$value$plusargs("vcd=%s", vcd_path)) begin
      $display("TB_ERROR: missing +vcd"); $finish;
    end
    void'($value$plusargs("cycle_limit=%d", cycle_limit));
    stim_fd = $fopen(stim_path, "r");
    obs_fd  = $fopen(obs_path, "w");
    if (stim_fd == 0 || obs_fd == 0) begin
      $display("TB_ERROR: cannot open files"); $finish;
    end
    $dumpfile(vcd_path);
    $dumpvars(0, tb);
    $fwrite(obs_fd, "{\"type\":\"observed_metadata\",\"case\":\"case_04_alu_xprop\",\"time_unit_ps\":1}\n");

    rst_n = 1'b0; in_valid = 1'b0; a = '0; b = '0; cfg_we = 1'b0; cfg_sel = 1'b0;
    @(negedge clk);
    stim_done = 1'b0;
    while (!stim_done) begin
      @(negedge clk);
      if (applied >= cycle_limit || !next_stim_line())
        stim_done = 1'b1;
      else begin
        rst_n = f_rst[0]; in_valid = f_iv[0]; a = f_a[7:0]; b = f_b[7:0];
        cfg_we = f_we[0]; cfg_sel = f_sel[0];
        applied += 1;
      end
    end
    #2;
    $fclose(stim_fd);
    $fclose(obs_fd);
    $finish;
  end

  longint t_edge;
  logic p_rst, p_iv, p_we, p_sel, p_ov, p_selq;
  logic [7:0] p_a, p_b, p_od;
  always @(posedge clk) begin
    p_rst = rst_n; p_iv = in_valid; p_a = a; p_b = b; p_we = cfg_we; p_sel = cfg_sel;
    p_ov = out_valid; p_od = out_data; p_selq = dut.sel_q;
    t_edge = $time;
    #1;
    $fwrite(obs_fd,
      "{\"type\":\"edge\",\"time_ps\":%0d,\"pre\":{\"rst_n\":\"%b\",\"in_valid\":\"%b\",\"a\":\"%b\",\"b\":\"%b\",\"cfg_we\":\"%b\",\"cfg_sel\":\"%b\",\"out_valid\":\"%b\",\"out_data\":\"%b\",\"sel_q\":\"%b\"},\"settled\":{\"out_valid\":\"%b\",\"out_data\":\"%b\",\"sel_q\":\"%b\"}}\n",
      t_edge, p_rst, p_iv, p_a, p_b, p_we, p_sel, p_ov, p_od, p_selq,
      out_valid, out_data, dut.sel_q);
  end
endmodule
