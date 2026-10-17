# iTerm2 Xshell-style Plugin — Yêu cầu

## 1. Mục tiêu

Biến iTerm2 trên macOS thành trải nghiệm giống **Xshell + Xftp**:

1. **Panel kênh SSH (Session Manager):** danh sách các kênh SSH đã lưu. Click vào là mở SSH. Có thể thêm/sửa/xoá/nhóm kênh. Tự import từ `~/.ssh/config`.
2. **Panel duyệt file (Xftp):** duyệt file của server mà tab hiện tại đang SSH vào. Upload (kéo thả), tải về, tạo thư mục, đổi tên, xoá.

Cả hai là **iTerm2 Python API script** (long-running daemon, AutoLaunch). UI là **webview tool trong Toolbelt**, phục vụ bởi một web server aiohttp chạy trên `127.0.0.1`.

## 2. Điểm xuất phát

- `XftpPanel.py` (đi kèm) là bản đầu của panel duyệt file. Bản này **chưa từng chạy trên iTerm2 thật**: mới test phần parse lệnh ssh và kiểm tra cú pháp Python/JS.
- Được phép refactor thoải mái. Gợi ý cấu trúc:

```
iterm-xshell/
  xshell_panels/            # package
    __init__.py
    ssh_parse.py            # parse lệnh ssh, ssh -G
    ssh_config_import.py    # đọc ~/.ssh/config
    sessions_store.py       # lưu/đọc danh sách kênh
    sftp_pool.py            # pool kết nối asyncssh
    terminal.py             # lớp bao iTerm2 API (context, mở tab, cd)
    web.py                  # aiohttp app, routes
    ui/sessions.html        # UI panel kênh SSH
    ui/files.html           # UI panel duyệt file
  XshellPanels.py           # entry point cho iTerm2 (run_forever)
  tests/
  install.sh                # cài vào ~/Library/Application Support/iTerm2/Scripts/AutoLaunch
  README.md
```

- Một process, một web server, hai webview tool (hai URL khác nhau, cùng token).
- Phụ thuộc: `iterm2`, `aiohttp`, `asyncssh`. Không thêm framework frontend; HTML/CSS/JS thuần, một file mỗi panel.

## 3. Panel kênh SSH (mới)

### 3.1 Vị trí

- Người dùng muốn panel này **ở bên trái**. Toolbelt của iTerm2 mặc định nằm bên phải. Việc đầu tiên: **kiểm tra iTerm2 có cho đặt Toolbelt bên trái không** (setting, hoặc API).
  - Nếu không, chấp nhận cả hai tool cùng nằm trong Toolbelt (xếp chồng) và ghi rõ hạn chế này trong README. Không tự làm window riêng nếu chưa hỏi lại.

### 3.2 Dữ liệu một kênh

| Trường | Bắt buộc | Ghi chú |
|---|---|---|
| `id` | có | uuid |
| `name` | có | tên hiển thị |
| `group` | không | thư mục/nhóm, ví dụ "Production", "Kho vận" |
| `host` | có | hostname, IP, hoặc alias trong ~/.ssh/config |
| `port` | không | mặc định 22 |
| `user` | không | |
| `identity_file` | không | đường dẫn key |
| `proxy_jump` | không | |
| `extra_args` | không | tham số ssh thêm, ví dụ `-L 8080:localhost:80` |
| `remote_dir` | không | thư mục mở sẵn sau khi vào |
| `color` / `tags` | không | để lọc và phân biệt môi trường |
| `source` | có | `manual` hoặc `ssh_config` |

- Lưu ở `~/.config/iterm-xshell/sessions.json`. Ghi atomic (ghi file tạm rồi rename), quyền `600`.
- **Không lưu mật khẩu dạng plaintext.** Nếu có lưu mật khẩu thì dùng macOS Keychain (lệnh `security add-generic-password` / `find-generic-password`), có tuỳ chọn bật/tắt. Mặc định dựa vào SSH key/agent.

### 3.3 Import từ `~/.ssh/config`

- Đọc các block `Host`, hỗ trợ `Include` (kể cả glob, đường dẫn tương đối theo `~/.ssh`).
- Bỏ qua pattern có `*`, `?`, `!` (wildcard không phải host cụ thể).
- Một dòng `Host a b c` tạo ra ba kênh.
- Kênh import chỉ cần lưu alias trong `host`. Lúc kết nối thì để ssh tự đọc config (`ssh alias`). Không copy HostName/Port vào kênh, để khi config đổi thì kênh tự đúng.
- Có nút **"Đồng bộ lại"**: thêm alias mới, đánh dấu alias đã biến mất khỏi config (không tự xoá), không đụng tới kênh `manual`.
- Tự đồng bộ khi khởi động và khi `~/.ssh/config` thay đổi (theo mtime).

### 3.4 UI

- Danh sách gom theo nhóm, nhóm thu gọn được. Có ô tìm kiếm lọc theo tên/host/tag (focus bằng ⌘F).
- **Click** (hoặc Enter) mở kênh trong **tab mới**. Menu chuột phải (tự vẽ bằng HTML) có thêm: mở ở split dọc, mở ở split ngang, mở ở tab hiện tại, sửa, nhân bản, xoá, copy lệnh ssh.
- Form thêm/sửa kênh ngay trong panel (modal HTML tự vẽ).
- Hiện badge cho kênh đang mở trong một tab nào đó.
- Giao diện sáng/tối theo hệ thống, gọn trong panel rộng khoảng 220–320px.

### 3.5 Mở kênh

- Tạo lệnh bằng `shlex.join` (không nối chuỗi tay), ví dụ `ssh -p 2222 -i key -J bastion user@host`. Nếu có `remote_dir` thì thêm `-t` và `"cd DIR && exec $SHELL -l"`.
- Mở bằng iTerm2 API: tab mới hoặc split trong cửa sổ hiện tại. Gửi lệnh qua `async_send_text`, hoặc tạo session với custom command (chọn cách nào để tab đóng bình thường khi ssh thoát).
- Đặt tên tab bằng tên kênh, đặt biến `user.xshellSessionId` cho session để biết tab nào thuộc kênh nào.
- Panel duyệt file phải nhận ra kênh vừa mở. Nếu tab có `user.xshellSessionId` thì dùng thông tin của kênh đó, không cần parse lệnh.

## 4. Panel duyệt file (giữ phạm vi của `XftpPanel.py`)

- Tự nhận host từ tab hiện tại: biến `user.xshellSessionId`, nếu không có thì parse `commandLine` (fallback `ps -o args= -p jobPid`). Resolve qua `ssh -G` (alias, Port, User, IdentityFile, ProxyJump, IdentityAgent).
- Theo thư mục terminal khi server có iTerm2 Shell Integration (biến `hostname` khác hostname local thì đọc biến `path`).
- Thao tác: duyệt, ↑, home, refresh, nhập path, upload (kéo thả và nút chọn file, có tiến độ, xác nhận ghi đè), tải về `~/Downloads` (cả thư mục, tự đổi tên nếu trùng, `open -R`), tạo thư mục, đổi tên, xoá (đệ quy, có xác nhận), "cd terminal tới đây", chọn nhiều bằng ⌘ và Shift.
- Pool kết nối theo (alias, user, port, jump), tự nối lại khi mất kết nối. Mật khẩu (nếu cần) chỉ giữ trong RAM, hoặc lấy từ Keychain nếu kênh có lưu.
- Kiểm tra host key bằng `~/.ssh/known_hosts`, báo lỗi dễ hiểu nếu host chưa có trong đó.
- **Nên có thêm:** kéo thả cả thư mục để upload, huỷ upload đang chạy, xem nhanh file text nhỏ.

## 5. Bảo mật

- Server chỉ bind `127.0.0.1`, cổng ngẫu nhiên, token ngẫu nhiên mỗi lần chạy. Mọi `/api/*` yêu cầu header `X-Token`.
- Tên file từ client luôn đi qua `basename`, chặn `..` và `/`.
- Không log mật khẩu hay nội dung file.
- Thao tác phá huỷ (xoá, ghi đè) luôn có xác nhận trên UI.

## 6. Những giả định cần kiểm chứng sớm trên iTerm2 thật

Bản đầu được viết khi chưa chạy thử, nên mỗi điểm dưới đây phải kiểm tra trước khi xây tiếp:

1. `iterm2.tool.async_register_web_view_tool(connection, name, id, reveal, url)` đúng chữ ký, và load được `http://127.0.0.1:<port>`.
2. Biến session `commandLine`, `jobPid`, `jobName`, `hostname`, `path` có tồn tại và có giá trị như mong đợi khi đang ssh.
3. Webview của Toolbelt có nhận **kéo thả file từ Finder** không.
4. Webview có chạy `confirm()`/`prompt()` không. Bản đầu đã cố tình tránh dùng chúng bằng modal tự vẽ; giữ nguyên cách này.
5. Process script có `SSH_AUTH_SOCK` không (asyncssh có dùng được ssh-agent không). Nếu không có thì lấy từ `launchctl getenv SSH_AUTH_SOCK`.
6. Ngữ nghĩa tham số `asyncssh.connect`: `client_keys`, `known_hosts=None`, `tunnel` dạng chuỗi ProxyJump, `agent_path`.
7. Toolbelt có đặt được bên trái không (mục 3.1).

Ghi kết quả từng điểm vào README (mục "Đã kiểm chứng").

## 7. Test

**Unit (pytest, không cần iTerm2):**
- Parse lệnh ssh: `-p`, `-p2222`, `-l`, `-o Port=`, `-o User=`, `-J`, `ssh://user@host:port`, user@host, các option có tham số, remote command phía sau, các lệnh không phải ssh.
- Import `~/.ssh/config`: nhiều host trên một dòng, wildcard, `Include` lồng nhau, comment, dùng `=`, viết hoa/thường lẫn lộn.
- Store: thêm/sửa/xoá, ghi atomic, quyền file, đồng bộ lại không làm mất kênh `manual`.
- Tạo lệnh ssh từ một kênh (escaping, `remote_dir`).

**Integration (không cần iTerm2):**
- Dựng SFTP server thật trên máy: Docker `linuxserver/openssh-server`, hoặc bật macOS Remote Login, hoặc asyncssh server ngay trong test. Gọi các API list/upload/download/mkdir/rename/delete bằng aiohttp test client, có file vài trăm MB, tên file có dấu cách và tiếng Việt, symlink tới thư mục.
- Mất kết nối giữa chừng thì tự nối lại.
- Server chỉ cho đăng nhập bằng mật khẩu thì API trả `code: "auth"`.

**E2E trên iTerm2 (bắt buộc, chạy thật):**
1. Cài bằng `install.sh`, khởi động lại iTerm2, cả hai tool hiện trong Toolbelt.
2. Kênh từ `~/.ssh/config` xuất hiện. Thêm một kênh tay, click vào thì mở tab mới và ssh thành công.
3. Panel file tự chuyển sang host đó. Khi chuyển qua lại giữa các tab ssh khác nhau, panel đổi theo.
4. Kéo thả file từ Finder để upload, tải về một thư mục, đổi tên, xoá, tạo thư mục.
5. `cd` trong terminal thì panel đi theo (có Shell Integration). Nút "cd ↵" chuyển được terminal.
6. Sửa `~/.ssh/config` thì danh sách kênh cập nhật.
7. Panel không lỗi khi cửa sổ không có tab ssh nào.

Chụp màn hình các bước E2E chính để làm bằng chứng.

## 8. Tiêu chí hoàn thành

- Toàn bộ unit và integration test pass. Checklist E2E trên iTerm2 đều đạt, có ảnh chụp.
- Mục 6 có kết luận cho từng giả định.
- `install.sh` chạy được trên máy sạch: tạo venv hoặc dùng runtime Python của iTerm2, cài phụ thuộc, copy vào AutoLaunch.
- README có: cài đặt, cách dùng, phím tắt, cấu hình, hạn chế còn lại.
