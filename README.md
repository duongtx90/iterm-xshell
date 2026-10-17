# iterm-xshell

Biến iTerm2 trên macOS thành trải nghiệm giống **Xshell + Xftp**:

- **Kênh SSH** (Session Manager): danh sách kênh đã lưu, gom theo nhóm, tự import từ `~/.ssh/config`. Click là mở SSH trong tab mới.
- **Xftp**: duyệt file trên server mà tab hiện tại đang ssh tới. Hỗ trợ upload bằng kéo thả, tải về, tạo thư mục, đổi tên, xoá, xem nhanh, và `cd` terminal tới thư mục đang xem.

Một daemon Python (iTerm2 Python API, AutoLaunch) chạy một web server aiohttp trên `127.0.0.1`, phục vụ hai panel HTML/JS thuần:

| Panel | Nằm ở đâu |
|---|---|
| Kênh SSH | **Cửa sổ riêng dock sát mép trái** cửa sổ iTerm2 đang active, vì Toolbelt không đặt bên trái được (xem *Đã kiểm chứng*). Tuỳ chọn: thêm vào Toolbelt. |
| Xftp | Tool `Xftp` trong Toolbelt (bên phải). |

## Cài đặt

```bash
./install.sh            # cài/cập nhật rồi khởi động ngay, không cần khởi động lại iTerm2
./install.sh --help     # --python PATH, --no-start, --dev, --uninstall, --purge
```

Cần có:
- macOS và iTerm2 3.5 trở lên (đã thử trên 3.7.3), với **Settings > General > Magic > Enable Python API** đang bật.
- Python 3.9 trở lên. install.sh tự tìm theo thứ tự: Homebrew, python.org, runtime Python của iTerm2, rồi `/usr/bin/python3` của Command Line Tools.

install.sh sẽ:
1. Tạo venv ở `~/Library/Application Support/iterm-xshell/venv` và cài `iterm2 aiohttp asyncssh pyobjc-framework-WebKit pyobjc-framework-Quartz`. Trên Apple Silicon luôn chạy với `arch -arm64`.
2. Copy mã nguồn vào `~/Library/Application Support/iterm-xshell/app` và tạo `run.sh`. `run.sh` thay instance cũ nếu đang chạy.
3. Biên dịch `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/XshellPanels.scpt`. Mỗi lần iTerm2 khởi động, file này xin cookie Python API bằng `request cookie and key` rồi gọi `run.sh`.

Muốn khởi động lại daemon thì chọn menu **Scripts > AutoLaunch > XshellPanels**, hoặc chạy lại `./install.sh`.
Log nằm ở `~/Library/Logs/iterm-xshell.log` (app) và `~/Library/Logs/iterm-xshell.out` (stdout/stderr).

Gỡ: `./install.sh --uninstall` giữ lại danh sách kênh, `--purge` xoá luôn.

## Cách dùng

### Kênh SSH (cửa sổ bên trái)

- Cửa sổ chỉ hiện khi iTerm2 đang ở trên cùng, và luôn bám sát mép trái cửa sổ iTerm2 đang active. Kéo cạnh phải để đổi độ rộng (lưu ở `~/.config/iterm-xshell/window.json`). Nếu bên trái không đủ chỗ (cửa sổ sát mép màn hình), cửa sổ iTerm2 được dịch sang phải một lần cho vừa.
- Click vào panel **không** lấy focus khỏi iTerm2 (non-activating panel), nhưng vẫn gõ được vào ô tìm kiếm và form.
- **Click** hoặc **Enter** mở kênh trong **tab mới**. **Shift+Enter** mở ở split dọc.
- **Chuột phải** (hoặc phím Menu) mở menu: mở tab mới, split dọc, split ngang, tab hiện tại, chuyển tới tab đang mở, sửa, nhân bản, copy lệnh ssh, xoá.
- Badge xanh `●` / `● 2` cho biết kênh đang mở trong 1 hoặc 2 tab.
- Kênh import từ `~/.ssh/config` có biểu tượng ⚙ và nằm trong nhóm `~/.ssh/config` (đổi nhóm được). Nếu alias biến mất khỏi config thì kênh bị gạch ngang và không mở được, nhưng không bị tự xoá.
- Nút ⟳ đồng bộ lại ngay. Ngoài ra daemon tự đồng bộ khi khởi động và khi `~/.ssh/config` hoặc file `Include` của nó thay đổi (kiểm tra mỗi 2 giây theo mtime/size).
- Bấm × trên thanh tiêu đề để ẩn cửa sổ. Bật lại bằng phím tắt (xem dưới).

### Xftp (Toolbelt)

Bật bằng **View > Toolbelt > Show Toolbelt**, rồi tick **Xftp** trong **View > Toolbelt**.

- Panel tự nhận host của tab hiện tại. Nếu tab được mở từ panel kênh SSH (có biến `user.xshellSessionId`), panel dùng đúng thông tin của kênh, kể cả `identity_file` và các `-o` trong tham số thêm. Nếu không, panel parse lệnh `ssh …` của tab rồi resolve bằng `ssh -G`.
- **⇵ theo terminal**: khi server có [iTerm2 Shell Integration](https://iterm2.com/documentation-shell-integration.html), `cd` trong terminal thì panel đi theo. Nút **cd ↵** làm ngược lại: `cd` terminal tới thư mục panel đang xem.
- Upload: kéo thả file **hoặc cả thư mục** vào danh sách, hoặc bấm ⤒. Có thanh tiến độ, nút **✕ Huỷ** (file dở dang bị xoá trên server) và xác nhận trước khi ghi đè.
- **Tải về** lưu vào `~/Downloads`, cả thư mục cũng được. Trùng tên thì tự đổi thành `tên (1)`, xong thì mở Finder ở file vừa tải.
- **Xem** (hoặc Space) xem nhanh 256 KB đầu của file text. File nhị phân bị từ chối.
- ⇄ kết nối tay tới alias hoặc `user@host:port` bất kỳ. Để trống thì quay về chế độ theo terminal.
- Server chỉ cho đăng nhập bằng mật khẩu thì panel hỏi mật khẩu. Mật khẩu chỉ giữ trong RAM, trừ khi kênh bật lưu Keychain.

### Phím tắt

| Ở đâu | Phím | Việc |
|---|---|---|
| Kênh SSH | ⌘F | Focus ô tìm kiếm (tìm không cần dấu: `kho van` khớp "Kho vận") |
| Kênh SSH | ↑ ↓, Enter, Shift+Enter | Chọn kênh, mở tab mới, mở split dọc |
| Kênh SSH | ⌘N | Thêm kênh |
| Kênh SSH | ⌫ | Xoá kênh (có xác nhận) |
| Kênh SSH | Esc | Xoá ô tìm kiếm, đóng menu/form |
| Xftp | ⌘↑ / Enter | Lên thư mục cha / vào thư mục |
| Xftp | ⌘A, ⌘-click, Shift-click | Chọn nhiều |
| Xftp | Space | Xem nhanh |
| Xftp | ⌫ | Xoá (có xác nhận) |
| Xftp | ⌘L | Nhập đường dẫn |

Phím tắt toàn cục để ẩn/hiện cửa sổ kênh SSH: vào **Settings > Keys > Key Bindings > +**, chọn Action **Invoke Script Function**, rồi nhập `xshell_toggle_sessions()` để ẩn/hiện, hoặc `xshell_focus_sessions()` để hiện và focus ô tìm kiếm.

## Cấu hình

Lưu ở `~/.config/iterm-xshell/sessions.json` (thư mục quyền `700`, file quyền `600`, ghi atomic bằng file tạm + `rename`). Phần `"settings"`:

| Khoá | Mặc định | Ý nghĩa |
|---|---|---|
| `download_dir` | `~/Downloads` | Thư mục tải về |
| `strict_host_key_checking` | `true` | Kiểm tra host key theo `UserKnownHostsFile` của `ssh -G`. `StrictHostKeyChecking no` trong config cũng được tôn trọng |
| `ssh_config` | `~/.ssh/config` | File config để import và để resolve (`ssh -F`) |
| `sessions_window` | `true` | Bật cửa sổ kênh SSH bên trái |
| `sessions_window_make_room` | `true` | Dịch cửa sổ iTerm2 sang phải khi bên trái không đủ chỗ |
| `sessions_in_toolbelt` | `false` | Thêm panel kênh SSH vào Toolbelt |
| `keychain_enabled` | `true` | Cho phép lưu mật khẩu kênh trong macOS Keychain |

Sửa xong thì khởi động lại daemon.

Một kênh gồm các trường `id, name, group, host, port, user, identity_file, proxy_jump, extra_args, remote_dir, color, tags, source (manual|ssh_config), missing, keychain, autotype_password`. Kênh import chỉ lưu alias trong `host`, còn HostName/Port/User do `ssh` đọc từ config lúc kết nối.

**Mật khẩu**: mặc định dựa vào SSH key/agent. Nếu bật "Lưu mật khẩu trong Keychain" cho một kênh, mật khẩu được lưu bằng `security` (service `iterm-xshell`, account là id của kênh) qua `security -i` đọc từ stdin, nên không lộ trên argv. Panel Xftp dùng mật khẩu này khi server đòi mật khẩu. Tuỳ chọn "Tự gõ mật khẩu" gõ nó vào terminal khi thấy dấu nhắc `password:` trong 30 giây đầu.

**Lệnh mở kênh**: dựng bằng `shlex.join`, ví dụ `ssh -p 2222 -i key -J bastion user@host`. Nếu có `remote_dir` thì thêm `-t … 'cd DIR && exec $SHELL -l'`. Tab mới dùng profile *custom command* `/bin/sh -c 'ssh …; …'`. Nhờ vậy tab đóng như bình thường khi ssh thoát; riêng khi ssh lỗi (mã 255) tab được giữ lại để đọc lỗi. Mỗi tab cũng sống tối thiểu 5 giây để tránh cảnh báo modal của iTerm2 (xem dưới). Chế độ "tab hiện tại" gửi lệnh ssh vào shell đang mở.

## Bảo mật

- Server chỉ bind `127.0.0.1` với cổng ngẫu nhiên, token ngẫu nhiên mỗi lần chạy, và mọi `/api/*` đều cần header `X-Token`. URL chứa token được truyền cho cửa sổ kênh SSH qua biến môi trường, không qua argv (để `ps` của user khác không thấy).
- Tên file từ client luôn qua `basename`. Đường dẫn thư mục con khi upload cả thư mục bị chặn `..`, rỗng và `\0`.
- Không log mật khẩu hay nội dung file.
- Xoá và ghi đè luôn có xác nhận bằng modal tự vẽ, vì `confirm()`/`prompt()` không hoạt động trong webview.

## Đã kiểm chứng (SPEC mục 6, iTerm2 3.7.3, macOS 26.5, Apple Silicon)

| # | Giả định | Kết luận |
|---|---|---|
| 1 | `iterm2.tool.async_register_web_view_tool(connection, name, id, reveal, url)` | **Đúng chữ ký**, trả về `None`. Webview tải được `http://127.0.0.1:<port>`, và `fetch` gửi kèm header tuỳ ý chạy được. Mỗi cửa sổ iTerm2 có một webview riêng, tải ngay cả khi Toolbelt đang ẩn. Khi daemon khởi động lại và đăng ký lại cùng `id` với URL mới, các webview đang mở tự chuyển sang URL mới. |
| 2 | Biến `commandLine`, `jobPid`, `jobName`, `hostname`, `path` | **Có.** Khi đang ssh: `commandLine='ssh xe-pay-243'`, `jobName='ssh'`, `jobPid` là pid của ssh. Server không có Shell Integration thì `hostname`/`path` vẫn là của máy local. Có Shell Integration (thử bằng container) thì `hostname` là tên server và `path` là thư mục remote, đổi ngay khi `cd`. Biến `user.*` chưa đặt thì trả `None`. |
| 3 | Kéo thả file từ Finder vào webview Toolbelt | **Chưa kiểm chứng trên GUI thật**, vì màn hình bị khoá suốt phiên làm việc. Đã kiểm phần JS bằng `DragEvent`/`DataTransfer` trong WKWebView (e2e/ui_snapshots.py). Kéo thả thật từ Finder nằm trong checklist còn lại. |
| 4 | `confirm()`/`prompt()` | **Không dùng được**: `confirm()` trả `false`, `prompt()` trả `null`, `alert()` không làm gì, không có hộp thoại nào hiện ra. Đã dùng modal tự vẽ ở cả hai panel. |
| 5 | `SSH_AUTH_SOCK` trong process script | **Có**: iTerm2 thừa hưởng `SSH_AUTH_SOCK` của launchd, trùng với `launchctl getenv SSH_AUTH_SOCK`. `run.sh` vẫn fallback sang `launchctl getenv`. Lưu ý `PATH` của iTerm2 chỉ có `/usr/bin:/bin:/usr/sbin:/sbin`, nên code gọi `/usr/bin/ssh`, `/usr/bin/security` bằng đường dẫn tuyệt đối. |
| 6 | Tham số `asyncssh.connect` (2.24) | `known_hosts=None` **tắt** kiểm tra host key, còn mặc định thì đọc `~/.ssh/known_hosts`. `client_keys` dùng sau key của agent. `tunnel` nhận chuỗi `[user@]host[:port]` hoặc chuỗi phân cách bằng dấu phẩy. `agent_path` mặc định lấy `SSH_AUTH_SOCK`, còn `None` thì tắt agent. **Phát hiện thêm**: asyncssh tự đọc `~/.ssh/config` và khớp theo HostName chứ không theo alias, nên code truyền `config=[]` sau khi đã resolve bằng `ssh -G`. Với ProxyJump, code không dùng chuỗi `tunnel` mà tự resolve từng chặng bằng `ssh -G` (để key/known_hosts của máy jump đúng như ssh) rồi nối bằng `tunnel=<conn>`. Integration test có server password-only, ProxyJump thật và host key lạ. |
| 7 | Toolbelt đặt bên trái | **Không được.** Không có setting, khoá defaults hay API nào. Chuỗi trợ giúp trong binary ghi: *"The toolbelt appears on the right side of your terminal window."* Theo quyết định của người dùng, panel kênh SSH được làm thành cửa sổ riêng dock bên trái. |

Phát hiện thêm khi chạy thật:
- **Script "full environment" của iTerm2 không dùng được venv tự dựng.** iTerm2 3.7 đòi `iterm2env-metadata.json` và runtime do chính nó tải. Thiếu thì nó hiện hộp thoại "Upgrade Python Runtime?" và không chạy script. Vì vậy install.sh dùng AutoLaunch `.scpt` + venv riêng, và luồng này đã được kiểm chứng: cookie API, `SSH_AUTH_SOCK`, đăng ký tool.
- **Cảnh báo modal "A session ended very soon after starting"**: khi một session dùng custom command kết thúc trong vài giây, iTerm2 hiện cảnh báo này (advanced setting `shortLivedSessionDuration`). Trong lúc nó mở, **toàn bộ Python API bị treo**, kể cả kết nối mới. Cách xử lý: wrapper giữ tab sống tối thiểu 5 giây, mọi RPC tới iTerm2 có timeout 4 giây, và panel báo "iTerm2 không phản hồi (có hộp thoại đang mở?)" thay vì treo.
- Trên Apple Silicon, Python universal2 được gọi từ một process x86 (ví dụ `timeout` của Homebrew Intel cũ) sẽ chạy dưới Rosetta và không load được wheel arm64. `install.sh`/`run.sh` vì vậy luôn chạy qua `arch -arm64`.
- Tên file trong `filename=` của multipart bị escape không thống nhất: WebKit chỉ đổi `"` thành `%22`, còn aiohttp client thì percent-encode toàn bộ. UI gửi thêm field `name` chứa tên gốc UTF-8.
- `sftp.get(..., recurse=True)` mặc định chép symlink thành symlink, nên dùng `follow_symlinks=True` để tải về được thư mục mà symlink trỏ tới.

## Test

```bash
./install.sh --dev --no-start          # hoặc tự tạo venv từ requirements-dev.txt
~/Library/Application\ Support/iterm-xshell/venv/bin/python -m pytest -q
```

- **Unit** (`tests/test_*.py`): parse lệnh ssh (`-p`, `-p2222`, `-l`, `-o Port=`, `-o User=`, `-J`, `ssh://`, option có tham số, remote command, lệnh không phải ssh); import `~/.ssh/config` (nhiều host một dòng, wildcard, `Include` lồng nhau/glob/tương đối/vòng lặp, comment, `=`, hoa thường lẫn lộn); store (CRUD, ghi atomic, quyền file, đồng bộ không mất kênh `manual`); dựng lệnh ssh (escaping, `remote_dir`); wrapper tab; hình học dock.
- **Integration** (`tests/integration/`): SFTP server asyncssh thật chạy trong test, gồm 3 server (key, chỉ mật khẩu, jump), gọi API bằng aiohttp test client. Các case: list/upload/download/mkdir/rename/delete, file 300 MB (sha256), tên có dấu cách + tiếng Việt + nháy, symlink tới thư mục và symlink hỏng, path traversal, upload cả thư mục, huỷ upload giữa chừng, mất kết nối thì tự nối lại, password-only trả `code: "auth"`, host key lạ trả `code: "hostkey"`, ProxyJump, API kênh SSH, đồng bộ config theo mtime.
- **UI** (`e2e/ui_snapshots.py`): nạp hai panel vào WKWebView offscreen (sáng/tối, rộng 280px) với backend thật, mô phỏng click/chuột phải/form/kéo thả, rồi chụp ảnh vào `e2e/screenshots/`.
- **E2E trên iTerm2** (`e2e/run_e2e.py`): lái iTerm2 thật qua Python API + HTTP API của daemon đã cài, dùng container `linuxserver/openssh-server` (một container có Shell Integration).

## Hạn chế còn lại

- Toolbelt luôn ở bên phải. Panel kênh SSH là cửa sổ riêng dock bên trái nên:
  - với cửa sổ iTerm2 full-screen kiểu native, panel đè lên mép trái nội dung terminal (không thể chia Space);
  - panel bám theo bằng cách hỏi vị trí mỗi 150 ms, nên khi kéo cửa sổ iTerm2 panel trễ một nhịp;
  - hotkey window (cửa sổ nổi) của iTerm2 không được dock.
- Key có passphrase mà không nạp vào ssh-agent (`ssh-add --apple-use-keychain`) thì panel Xftp không dùng được (asyncssh bỏ qua key mã hoá). Terminal vẫn ssh bình thường.
- `ProxyCommand` được truyền sang asyncssh (thay `%h %p %r %n`). `Match exec` và các tính năng chỉ OpenSSH có vẫn được `ssh -G` resolve, nhưng asyncssh có thể không hỗ trợ hết thuật toán/cipher hiếm.
- Panel Xftp đi theo thư mục terminal chỉ khi server có Shell Integration.
- "Tự gõ mật khẩu" chỉ nhận dấu nhắc kiểu `password:` (không nhận OTP/2FA).
- Mỗi lần iTerm2 khởi động, script AutoLaunch xin cookie qua AppleScript. Lần đầu macOS có thể hỏi quyền Automation.
