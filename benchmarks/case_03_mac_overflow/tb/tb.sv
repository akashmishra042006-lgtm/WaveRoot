// case_03_mac_overflow shared harness. Same conventions as case_01's tb.sv.
// Stimulus line: `rst_n in_valid a_hex b_hex` (a and b are 8-bit two's
// complement, written as two hex digits).
`timescale 1ps/1ps
module tb;
  localparam int unsigned HALF_PERIOD = 5000;

  logic               clk;
  logic               rst_n;
  logic               in_valid;
  logic signed  [7:0] a;
  logic signed  [7:0] b;
  logic               out_valid;
  logic signed [15:0] out_data;

  mac dut (
    .clk(clk), .rst_n(rst_n),
    .in_valid(in_valid), .a(a), .b(b),
    .out_valid(out_valid), .out_data(out_data)
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
  logic [31:0] f_rst, f_iv;
  logic [31:0] f_a, f_b;

  function automatic bit next_stim_line();
    forever begin
      code = $fgets(line, stim_fd);
      if (code <= 0) return 0;
      code = $sscanf(line, " %b %b %h %h", f_rst, f_iv, f_a, f_b);
      if (code == 4) return 1;
      if (code > 0) begin
        $display("TB_ERROR: malformed stimulus line: %s", line);
        return 0;
      end
    end
    return 0;
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
    $fwrite(obs_fd, "{\"type\":\"observed_metadata\",\"case\":\"case_03_mac_overflow\",\"time_unit_ps\":1}\n");

    rst_n = 1'b0; in_valid = 1'b0; a = '0; b = '0;
    @(negedge clk);
    forever begin
      @(negedge clk);
      if (applied >= cycle_limit) break;
      if (!next_stim_line()) break;
      rst_n = f_rst[0]; in_valid = f_iv[0]; a = f_a[7:0]; b = f_b[7:0];
      applied += 1;
    end
    #2;
    $fclose(stim_fd);
    $fclose(obs_fd);
    $finish;
  end

  longint t_edge;
  logic p_rst, p_iv, p_ov;
  logic [7:0] p_a, p_b;
  logic [15:0] p_od, p_acc;
  always @(posedge clk) begin
    p_rst = rst_n; p_iv = in_valid; p_a = a; p_b = b;
    p_ov = out_valid; p_od = out_data; p_acc = dut.acc_q;
    t_edge = $time;
    #1;
    $fwrite(obs_fd,
      "{\"type\":\"edge\",\"time_ps\":%0d,\"pre\":{\"rst_n\":\"%b\",\"in_valid\":\"%b\",\"a\":\"%b\",\"b\":\"%b\",\"out_valid\":\"%b\",\"out_data\":\"%b\",\"acc_q\":\"%b\"},\"settled\":{\"out_valid\":\"%b\",\"out_data\":\"%b\",\"acc_q\":\"%b\"}}\n",
      t_edge, p_rst, p_iv, p_a, p_b, p_ov, p_od, p_acc,
      out_valid, out_data, dut.acc_q);
  end
endmodule
