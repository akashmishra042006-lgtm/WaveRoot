// case_02_axi_skid shared harness. Same conventions as case_01's tb.sv:
// clock rises at 5000 ps + k*10000 ps; reset released by stimulus line 0 at
// the falling edge at 20000 ps; inputs driven on falling edges; observed
// JSONL records `pre` (active region at the edge) and `settled` (edge+1 ps).
// Stimulus line: `rst_n s_valid s_data_hex m_ready`.
`timescale 1ps/1ps
module tb;
  localparam int unsigned HALF_PERIOD = 5000;

  logic       clk;
  logic       rst_n;
  logic       s_valid;
  logic       s_ready;
  logic [7:0] s_data;
  logic       m_valid;
  logic       m_ready;
  logic [7:0] m_data;

  elastic dut (
    .clk(clk), .rst_n(rst_n),
    .s_valid(s_valid), .s_ready(s_ready), .s_data(s_data),
    .m_valid(m_valid), .m_ready(m_ready), .m_data(m_data)
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
  logic [31:0] f_rst, f_sv, f_mr;
  logic [31:0] f_sd;

  function automatic bit next_stim_line();
    forever begin
      code = $fgets(line, stim_fd);
      if (code <= 0) return 0;
      code = $sscanf(line, " %b %b %h %b", f_rst, f_sv, f_sd, f_mr);
      if (code == 4) return 1;
      if (code > 0) begin
        $fatal(1, "TB_ERROR: malformed stimulus line: %s", line);
        return 0;
      end
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
    $fwrite(obs_fd, "{\"type\":\"observed_metadata\",\"case\":\"case_02_axi_skid\",\"time_unit_ps\":1}\n");

    rst_n = 1'b0; s_valid = 1'b0; s_data = '0; m_ready = 1'b0;
    @(negedge clk);
    forever begin
      @(negedge clk);
      if (applied >= cycle_limit) break;
      if (!next_stim_line()) break;
      rst_n = f_rst[0]; s_valid = f_sv[0]; s_data = f_sd[7:0]; m_ready = f_mr[0];
      applied += 1;
    end
    #2;
    $fclose(stim_fd);
    $fclose(obs_fd);
    $finish;
  end

  longint t_edge;
  logic p_rst, p_sv, p_sr, p_mv, p_mr;
  logic [7:0] p_sd, p_md;
  always @(posedge clk) begin
    p_rst = rst_n; p_sv = s_valid; p_sd = s_data; p_mr = m_ready;
    p_sr = s_ready; p_mv = m_valid; p_md = m_data;
    t_edge = $time;
    #1;
    $fwrite(obs_fd,
      "{\"type\":\"edge\",\"time_ps\":%0d,\"pre\":{\"rst_n\":\"%b\",\"s_valid\":\"%b\",\"s_data\":\"%b\",\"m_ready\":\"%b\",\"s_ready\":\"%b\",\"m_valid\":\"%b\",\"m_data\":\"%b\"},\"settled\":{\"m_valid\":\"%b\",\"m_data\":\"%b\",\"s_ready\":\"%b\"}}\n",
      t_edge, p_rst, p_sv, p_sd, p_mr, p_sr, p_mv, p_md,
      m_valid, m_data, s_ready);
  end
endmodule
