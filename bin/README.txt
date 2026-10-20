kb-mcp MCP Server —— 开箱即用包
================================

内容
----
  kb-mcp.exe        MCP server + 完整 kb 引擎（stdio / HTTP，无需 Python）
  README.txt        本文件
  1.安装初始化.cmd   自检（--version + 三行 stdio 握手；用临时数据目录，不碰真实库）
  2.启动MCP.cmd      HTTP 模式启动（streamable HTTP，默认 127.0.0.1:8000/mcp）
  3.启动管理端       HTTP管理端(默认 127.0.0.1:8000/mcp)
  .mcp.json         stdio 接入样例——command 路径是占位，按实际位置改
  .gitignore        运行产物忽略规则（data/、*.db 等；exe 本身入库）

使用（stdio：Claude Code / Cline 等 MCP 客户端）
----
  客户端里把 command 指到 kb-mcp.exe 的绝对路径，args 填 ["serve"]（见 .mcp.json）。
  手工验证：把三行 JSON-RPC 存成 req.txt，然后
    kb-mcp.exe serve < req.txt
  三行内容：
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}
    {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
    {"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}
  注意：serve 子命令不能省（裸 exe 只打印用法）；第二条 initialized
  通知也不能省——缺了它 tools/list 不会应答；stdin 关闭后进程自行退出
  （rc=0），不需要 shutdown 行。

使用（HTTP：本机 Cline 的接法，多程序共用）
----
  1. token 文件（凭据不进本目录）：%USERPROFILE%\.kb-mcp-http-token
     没有时先建，内容就是 Bearer token（PowerShell 一行）：
       Set-Content -NoNewline '%USERPROFILE%\.kb-mcp-http-token' ([guid]::NewGuid().ToString('N'))
  2. 双击 启动HTTP.cmd：起 exe，端点 http://127.0.0.1:8000/mcp。
     Cline 条目：type=streamableHttp，url=http://127.0.0.1:8000/mcp，
     headers.Authorization=Bearer <token>（与文件内容一致）。
  3. 端口用 KB_MCP_HTTP_PORT 覆盖；缺 token 文件时脚本明确报错退出。
  4. 停止服务：关掉窗口或 Ctrl-C；本服务没有额外的 shutdown 开关。

数据根
----
  默认：exe 同级 data\（首次跑真实命令时自动建立）。
  优先级：KB_MCP_HOME 环境变量 > 配置文件 data_dir > exe 同级 data\。
  启动HTTP.cmd 默认把 KB_MCP_HOME 指回 %USERPROFILE%\.local\share\kb-mcp
  （指向已有数据；不需要就自己覆盖这个变量）。
  stdio 接法下不设 KB_MCP_HOME 时数据落在 exe 同级 data\；想让客户端共用
  现有数据，在客户端配置的 env 里加 KB_MCP_HOME。

注意事项
--------
- 本包不含文档转换：docx / pdf / xlsx / pptx 等 -> Markdown 的转换继续在
  kbmcp 仓库侧用 tools\import-docs.cmd（venv 工作流）做；转出的 Markdown
  用 kb-mcp.exe import <目录> 或 MCP 工具导入，一样进本包服务的数据。
- kb import 只吃 Markdown 目录；按 frontmatter 解析，非 .md 文件跳过。
- 首启会自解压到临时目录，启动稍慢属正常；每次重建 exe 都是新二进制，
  Windows Defender 首次扫描会让首启更慢。
- exe 位置即工作根：README / 启动器 / 默认数据目录都以 exe 所在目录为基准。
  整个目录可以搬走，但别只把 exe 单独拎走。