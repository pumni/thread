# 📑 BÁO CÁO TOÀN DIỆN VỀ KIẾN TRÚC, TÍNH NĂNG VÀ LUỒNG XỬ LÝ DỰ ÁN CHROMIUM-TOOLFBPC

> **Dự án**: Chromium-ToolFbPC (Facebook Automation System)  
> **Phiên bản phân tích**: 2.0.0+  
> **Mục tiêu**: Hệ thống tự động hóa Facebook đa tài khoản, tương tác nuôi nick, đăng bài, bình luận, kết bạn, cào dữ liệu thông minh và tích hợp điều khiển thời gian thực qua WebSocket & CRM.  
> **Thư mục lưu trữ báo cáo**: `thread/`  

---

## MỤC LỤC

1. [TỔNG QUAN HỆ THỐNG VÀ KIẾN TRÚC TỔNG THỂ](#1-tổng-quan-hệ-thống-và-kiến-trúc-tổng-thể)
2. [CẤU TRÚC THƯ MỤC VÀ VAI TRÒ TỪNG MODULE](#2-cấu-trúc-thư-mục-và-vai-trò-từng-module)
3. [DANH MỤC TÍNH NĂNG CHI TIẾT](#3-danh-mục-tính-năng-chi-tiết)
   - 3.1. [Tự động hóa hành vi người dùng (Nuôi nick)](#31-tự-động-hóa-hành-vi-người-dùng-nuôi-nick)
   - 3.2. [Tương tác theo chỉ lệnh thời gian thực (WebSocket & CRM)](#32-tương-tác-theo-chỉ-lệnh-thời-gian-thực-websocket--crm)
   - 3.3. [Hệ thống cào dữ liệu thông minh (FacebookCommentScraper)](#33-hệ-thống-cào-dữ-liệu-thông-minh-facebookcommentscraper)
   - 3.4. [Quản lý tài khoản, đồng bộ MongoDB & Quản trị Proxy](#34-quản-lý-tài-khoản-đồng-bộ-mongodb--quản-trị-proxy)
   - 3.5. [Cơ chế chạy nền, ẩn danh và chống phát hiện (Anti-Detect)](#35-cơ-chế-chạy-nền-ẩn-danh-và-chống-phát-hiện-anti-detect)
4. [SƠ ĐỒ VÀ PHÂN TÍCH CÁC LUỒNG XỬ LÝ CHÍNH (WORKFLOWS)](#4-sơ-đồ-và-phân-tích-các-luồng-xử-lý-chính-workflows)
   - 4.1. [Luồng khởi động và vòng lặp hoạt động chính](#41-luồng-khởi-động-và-vòng-lặp-hoạt-động-chính)
   - 4.2. [Luồng kết nối WebSocket và cơ chế ưu tiên ngắt (Preemptive Interruption)](#42-luồng-kết-nối-websocket-và-cơ-chế-ưu-tiên-ngắt-preemptive-interruption)
   - 4.3. [Luồng đăng bài viết tự động (Post News Feed)](#43-luồng-đăng-bài-viết-tự-động-post-news-feed)
   - 4.4. [Luồng bình luận và trả lời phân cấp (Comment & Reply Hierarchy)](#44-luồng-bình-luận-và-trả-lời-phân-cấp-comment--reply-hierarchy)
   - 4.5. [Luồng cào comment tự động và đồng bộ cấu trúc Post](#45-luồng-cào-comment-tự-động-và-đồng-bộ-cấu-trúc-post)
5. [CẤU TRÚC DỮ LIỆU VÀ CÁC ĐỊNH DẠNG PROTOCOL](#5-cấu-trúc-dữ-liệu-và-các-định-dạng-protocol)
   - 5.1. [Cấu trúc Post Structure (`post_structure.json`)](#51-cấu-trúc-post-structure-post_structurejson)
   - 5.2. [Cấu trúc User Accounts (`user_accounts.json`)](#52-cấu-trúc-user-accounts-user_accountsjson)
   - 5.3. [Giao thức WebSocket Payload](#53-giao-thức-websocket-payload)
   - 5.4. [Cấu hình môi trường (`.env`)](#54-cấu-hình-môi-trường-env)
6. [ĐÁNH GIÁ KỸ THUẬT, ĐIỂM MẠNH VÀ KHUYẾN NGHỊ TỐI ƯU](#6-đánh-giá-kỹ-thuật-điểm-mạnh-và-khuyến-nghị-tối-ưu)

---

## 1. TỔNG QUAN HỆ THỐNG VÀ KIẾN TRÚC TỔNG THỂ

Dự án **Chromium-ToolFbPC** là một giải pháp tự động hóa trình duyệt chuyên biệt dành cho nền tảng Facebook chạy trên môi trường Windows. Hệ thống vận hành theo mô hình phân tán Client - Server:

- **Bên A (Master Server / CRM / WebSocket Server)**: Địa chỉ `wss://socket.hungha365.com:4000` và `https://socket.hungha365.com:4000/api`. Đóng vai trò máy chủ điều khiển, phát lệnh đăng bài, bình luận, yêu cầu cào dữ liệu và tiếp nhận phản hồi từ các bot.
- **Bên B (Client Bots / Tool PC)**: Các máy trạm hoặc tiến trình chạy `toolfacebook.py` sử dụng Google Chrome (qua Selenium WebDriver & Selenium Wire) kết nối trực tiếp vào Facebook bằng Profile Chrome độc lập (đã lưu sẵn phiên đăng nhập).
- **Cơ sở dữ liệu MongoDB trung tâm**: Lưu trữ danh sách thiết bị (`devices_fb_pc` / `devices`), quản lý phân bổ tài khoản trên từng máy trạm vật lý (`SERVER_NAME`).
- **Lưu trữ cục bộ (Local Storage)**: File cấu hình `user_accounts.json`, cache quan hệ phân cấp bài viết `post_structure.json` tại thư mục `%APPDATA%`, và cookie dự phòng.

```
       +-------------------------------------------------------+
       |             HỆ THỐNG QUẢN LÝ TRUNG TÂM                |
       |  - CRM Server: https://socket.hungha365.com:4000/api  |
       |  - WebSocket:  wss://socket.hungha365.com:4000        |
       |  - MongoDB:    Database Facebook / Collection devices |
       +---------------------------+---------------------------+
                                   ^
                                   | (WebSocket duplex / REST API)
                                   v
       +-------------------------------------------------------+
       |           MÁY TRẠM CLIENT (Windows PC)                |
       |                   toolfacebook.py                     |
       +---------------------------+---------------------------+
             |                     |                     |
             v                     v                     v
    +-----------------+   +-----------------+   +-----------------+
    | Chrome Profile  |   | Chrome Profile  |   | Chrome Profile  |
    |   Instance 1    |   |   Instance 2    |   |   Instance 3    |
    | (toolfacebook1) |   | (toolfacebook2) |   | (toolfacebook3) |
    +-----------------+   +-----------------+   +-----------------+
```

---

## 2. CẤU TRÚC THƯ MỤC VÀ VAI TRÒ TỪNG MODULE

```
D:\Dev\Chromium-ToolFbPC\
├── api/
│   └── api_service.py           # FastAPI service kích hoạt tool qua HTTP
├── thread/                      # Thư mục chứa báo cáo và tài liệu phân tích
├── venv/                        # Python Virtual Environment
├── .env                         # Biến môi trường: SERVER_NAME, Profile Dirs, MongoDB
├── api.py                       # REST API client gửi Post, Comment, Reply lên CRM
├── check_proxy.py               # Kiểm tra danh sách Proxy từ NCC với user_accounts.json
├── convert.py                   # Tiện ích chuyển đổi cookie từ .pkl sang .json
├── cookies4.pkl / fb_cookies.json# File cookie mẫu và cookie kết xuất
├── post_structure.json          # File cấu trúc lưu vết Post -> Comment -> Reply
├── redistribute_accounts.py     # Script chia ngẫu nhiên 38 tài khoản thành 3 nhóm
├── requirements.txt             # Danh sách thư viện phụ thuộc
├── test_all_proxies.py          # Kiểm tra đồng loạt danh sách Proxy
├── test_new_proxy.py            # Kiểm tra một cụm Proxy mới
├── test_proxy.py                # Script kiểm thử kết nối Proxy qua httpbin.org
├── toolfacebook_lib.py          # Thư viện phụ trợ kết nối backend cũ và Facebook DOM
├── toolfacebook.py              # MODULE LÕI CHÍNH: Quản trị vòng đời Bot, WebSocket, DOM
├── toolfacebook1.py             # Phiên bản chạy song song cho Profile 1
├── toolfacebook2.py             # Phiên bản chạy song song cho Profile 2
├── toolfacebook3.py             # Phiên bản chạy song song cho Profile 3
├── toolfacebook.spec            # PyInstaller build spec cho toolfacebook.exe
├── chat365vip.spec              # PyInstaller build spec cho chat365vip.exe
├── user_accounts.json           # Danh sách 38 tài khoản Facebook, proxy, role WebSocket
├── user_accounts_backup.json    # Bản sao lưu tài khoản trước khi phân nhóm
└── utils.py                     # Thư viện tiện ích Windows, logging màu, sync MongoDB
```

### Chi tiết vai trò các file mã nguồn chính:

1. **`toolfacebook.py` (4.900+ dòng)**:
   - Là "trái tim" của hệ thống.
   - Quản trị toàn bộ kết nối WebSocket 2 chiều với server.
   - Quản trị trình duyệt Selenium (Chrome).
   - Triển khai toàn bộ hành vi: lướt feed (`surf_facebook`), xem video (`watch_videos`), thả cảm xúc ngẫu nhiên (`react_post`), bình luận (`comment_post`), chia sẻ (`share_post`), kết bạn theo nhóm tuyển dụng (`add_friend`), nhắn tin (`send_message`), đăng bài theo lệnh socket (`post_news_feed`), bình luận theo URL (`comment_on_post_url`), trả lời bình luận (`reply_to_comment`), trả lời reply (`reply_to_reply_comment`).
   - Tích hợp lớp cào dữ liệu `FacebookCommentScraper` trích xuất thông tin người dùng và phân cấp bình luận.
   - Cơ chế cờ ngắt `stop_browsing`: khi bot đang lướt dạo hoặc xem video mà có lệnh mới từ WebSocket (đăng bài, bình luận), bot lập tức dừng hành động hiện tại để phục vụ lệnh tức thời.

2. **`toolfacebook1.py`, `toolfacebook2.py`, `toolfacebook3.py`**:
   - Nhân bản từ `toolfacebook.py`, chỉ khác duy nhất biến chỉ định Profile Chrome: `CHROME_USER_DATA_DIR_1`, `CHROME_USER_DATA_DIR_2`, `CHROME_USER_DATA_DIR_3`.
   - Cho phép mở đồng thời nhiều cửa sổ Chrome độc lập trên cùng một máy mà không bị xung đột khóa dữ liệu (profile lock).

3. **`utils.py` (376 dòng)**:
   - `normalize_env_value` & `get_env_value`: Đọc file `.env` an toàn, xử lý chuỗi raw string trên Windows.
   - `update_device_fb_pc_facebook_id`: Đọc Facebook ID thực tế sau khi bot mở trình duyệt, đối soát với danh sách tài khoản theo `SERVER_NAME` trong collection MongoDB `devices` và cập nhật trường `fb_id`.
   - `type_text_input`: Giả lập hành vi nhập liệu của con người (gõ từng ký tự với delay ngẫu nhiên `0.1s - 0.4s`).
   - `smooth_scroll`: Cuộn trang mượt mà 30 FPS chia nhỏ theo thời gian để né bot detector.
   - `hide_process`: Sử dụng Windows API (`ctypes`, `win32gui`, `win32process`) để ẩn cửa sổ console và gán tiến trình thành background system process (ưu tiên `BELOW_NORMAL_PRIORITY_CLASS`).
   - `run_as_trusted`: Khởi chạy lại với quyền Administrator (`runas`).
   - Hệ thống Logging đa sắc (`colorlog`) đồng thời ghi file log tại `%APPDATA%/Assets_f/toolfacebook.log`.

4. **`api.py` (60 dòng)**:
   - Triển khai các API Client Async qua thư viện `aiohttp`:
     - `create_post(post_data)`: Gửi thông tin bài viết đã đăng thành công lên CRM.
     - `create_comment(comment_data)`: Lưu comment (cả tự tạo và cào được) vào cơ sở dữ liệu.
     - `create_reply_comment(facebook_comment_id, reply_comment_data)`: Lưu phản hồi của bình luận.

5. **`toolfacebook_lib.py` (229 dòng)**:
   - Giao tiếp với API nội bộ `http://192.168.0.123:5000/`.
   - `get_answer(driver, group_link)`: Tự động phát hiện modal câu hỏi khi xin vào nhóm Facebook kín, gửi câu hỏi về server để lấy câu trả lời và tự động điền form (textbox, checkbox, radio button).
   - `extract_post_link(driver, post)`: Lấy link bài viết thông qua việc bấm nút Share -> "Sao chép liên kết" vào Clipboard (`pyperclip.paste()`).
   - `parse_comment_count`: Phân tích chuỗi số lượng comment (chuyển đổi đơn vị K, M).
   - `check_post`: Kiểm tra các bài viết đăng trong nhóm xem đã được phê duyệt chưa.

6. **`api/api_service.py` (20 dòng)**:
   - Dịch vụ FastAPI cung cấp endpoint `POST /login`.
   - Khi nhận được `user_id_chat`, tìm tài khoản trong `user_accounts.json` và khởi chạy tool thông qua `BackgroundTasks`.

7. **`redistribute_accounts.py` (69 dòng)**:
   - Tự động backup `user_accounts.json` thành `user_accounts_backup.json`.
   - Xáo trộn ngẫu nhiên và phân bổ 38 tài khoản vào 3 nhóm phụ trách:
     - Nhóm `22773024` (A Hán): 15 tài khoản.
     - Nhóm `22615815` (C. Phượng): 15 tài khoản.
     - Nhóm `22614471` (Chị Liên): 8 tài khoản.
   - Cập nhật trường `"to"` trong cấu hình WebSocket để điều phối tin nhắn về đúng quản lý.

---

## 3. DANH MỤC TÍNH NĂNG CHI TIẾT

### 3.1. Tự động hóa hành vi người dùng (Nuôi nick)
- **Lướt Bảng tin (Newfeed - `surf_facebook`)**:
  - Tự động truy cập trang chủ `https://www.facebook.com`.
  - Cuộn trang từ 20 đến 30 lần với gia số tọa độ ngẫu nhiên từ 600px - 1000px thông qua thuật toán cuộn mượt (`smooth_scroll`).
  - Dừng ngẫu nhiên 4 - 6 giây tại mỗi điểm để giả lập hành vi "đọc bài viết".
  - Theo chu kỳ cuộn (toán tử modulo):
    - Chia hết cho 7: Thả cảm xúc (`react_post`).
    - Chia hết cho 17: Chia sẻ bài viết (`share_post`).
    - Chia hết cho 13: Bình luận bài viết theo danh sách mẫu (`COMMENTS`).
- **Thả Reaction đa dạng (`react_post`)**:
  - Tìm nút Like (`Thích` / `Like`).
  - Di chuột (`ActionChains.move_to_element`) để mở thanh cảm xúc nổi.
  - Chọn ngẫu nhiên 1 trong các cảm xúc: `Like`, `Love`, `Care`, `Haha`, `Wow`.
  - Tìm chính xác nút cảm xúc tương ứng qua XPath hoặc Aria-label và click.
- **Xem Video Facebook Watch (`watch_videos`)**:
  - Truy cập `https://www.facebook.com/watch/`.
  - **Tắt toàn bộ âm thanh**: Ngay lập tức thực thi JavaScript tắt tiếng toàn bộ thẻ `<video>` và `<audio>` (`media.muted = true; media.volume = 0;`).
  - Cuộn xem từ 6 đến 15 video, mỗi video dừng từ 15 đến 40 giây.
  - Thả reaction hoặc chia sẻ theo chu kỳ.
  - Tự động nhận diện và click nút "Thẻ tiếp theo" (Next Card) để chuyển video tiếp theo.
- **Kết bạn theo nhóm tuyển dụng (`add_friend`)**:
  - Tìm kiếm các nhóm theo từ khóa: `https://www.facebook.com/search/groups?q=tuyển%20dụng`.
  - Chọn một nhóm ngẫu nhiên, truy cập trang `/members`.
  - Cuộn trang 5 lần để tải danh sách thành viên.
  - Trích xuất danh sách thành viên, lọc các thẻ profile hợp lệ và nút "Kết bạn với..." ("Add Friend").
  - Gửi lời mời kết bạn ngẫu nhiên.
  - **Kiểm soát hạn mức an toàn**: Giới hạn tối đa **10 lời mời/ngày** (`MAX_FRIEND_REQUESTS_PER_DAY = 10`). Tự động đặt lại counter sang ngày mới (`reset_friend_request_counter`). Báo cáo trạng thái kết bạn về WebSocket.
  - Tự động gửi tin nhắn chào hỏi và giới thiệu cổng thông tin việc làm `Timviec365`.
- **Nhắn tin cho bạn bè (`list_friend` & `send_message`)**:
  - Truy cập `https://www.facebook.com/friends/list`.
  - Thu thập danh sách bạn bè, chọn ngẫu nhiên một người và gửi tin nhắn qua khung chat Messenger nền web.

---

### 3.2. Tương tác theo chỉ lệnh thời gian thực (WebSocket & CRM)
Hệ thống duy trì một luồng ngầm liên tục kết nối tới WebSocket Server:

- **Đăng ký định danh (Client Registration)**:
  - Khi kết nối thành công, bot gửi bản tin `{"type": "register", "clientId": "<roleWebSocket>"}`.
- **Đăng bài viết mới theo lệnh (`type: new_post`)**:
  - Nhận nội dung văn bản (`content`), danh sách tệp đính kèm (`attachments`), và ID bài viết hệ thống (`postId`).
  - Tự động tải các tệp ảnh đính kèm từ CDN về thư mục cục bộ qua `download_image()`.
  - Mở trang chủ, tìm nút tạo bài viết hoặc nút "Ảnh/video".
  - Nạp toàn bộ đường dẫn ảnh cục bộ vào thẻ `<input type="file">`.
  - Đợi DOM render preview ảnh hoàn tất (tối đa 10 lần kiểm tra).
  - Nhập nội dung văn bản vào khối soạn thảo contenteditable (`div[dir='auto']` hoặc thẻ `<p>`).
  - Nhấn nút Đăng bài (Post) với cơ chế thử lại 3 lần (Click thông thường -> Click JavaScript -> ActionChains).
  - Dò tìm bài viết vừa xuất hiện trên feed (thẻ có `aria-posinset='1'`), tìm thẻ timestamp chứa tham số URL `__cft__`.
  - Click vào timestamp để lấy URL bài viết thực tế.
  - Gọi API `create_post` lưu bài viết vào CRM.
  - Cập nhật thông tin vào file cấu trúc `post_structure.json`.
  - Gửi thông báo hoàn tất kèm link bài viết về WebSocket (`type: URL_post`).
  - Xóa sạch các ảnh tạm đã tải về máy (`delete_image`).
- **Bình luận vào bài viết chỉ định (`type: comment`)**:
  - Nhận link bài viết `URL`, nội dung comment `content`, `postId`.
  - Điều hướng tới URL bài viết, tìm nút "Viết bình luận" (có bộ lọc thông minh bỏ qua nút media/ảnh).
  - Nhập văn bản vào comment box và gửi phím `Enter`.
  - Bắt timestamp của comment vừa đăng để trích xuất `comment_id`.
  - Ghi nhận vào `post_structure.json`, gọi API `create_comment`, gửi báo cáo `comment_result` về WebSocket.
- **Trả lời bình luận cấp 1 (`type: reply_comment`)**:
  - Nhận URL bài viết, ID bình luận mục tiêu `commentId`, nội dung trả lời `content`.
  - Tìm chính xác container bình luận mang class `x18xomjl xbcz3fp` chứa thẻ link `comment_id=<ID>`.
  - Bấm nút "Trả lời" (Reply) thuộc đúng container đó.
  - Gõ nội dung và gửi Enter.
  - Trích xuất `reply_comment_id` từ URL vừa sinh ra trong DOM.
  - Lưu vào nhánh `replies` của comment tương ứng trong `post_structure.json`.
  - Gọi API `create_reply_comment` và báo cáo socket `reply_to_comment_result`.
- **Trả lời phản hồi cấp 2 (`type: reply_reply_comment`)**:
  - Nhận `commentId`, `replyId`, nội dung trả lời.
  - Xác định container con `x6s0dn4 x3nfvp2`, thực hiện trả lời lồng nhau và đồng bộ hệ thống.
- **Tương tác nhóm theo lệnh CRM**:
  - `post_to_group`: Tải ảnh đính kèm từ CRM, truy cập group link, điền nội dung và đăng bài.
  - `join_group`: Tự động tham gia nhóm, kết hợp `toolfacebook_lib.get_answer` giải quyết bộ câu hỏi kiểm duyệt tự động.

---

### 3.3. Hệ thống cào dữ liệu thông minh (`FacebookCommentScraper`)
Lớp `FacebookCommentScraper` đóng vai trò là một crawler chuyên nghiệp:

- **Chuyển chế độ hiển thị comment**:
  - Tự động tìm và bấm vào dropdown sắp xếp bình luận.
  - Chuyển từ "Phù hợp nhất" (Most relevant) sang **"Tất cả bình luận" (All comments)** để không bỏ sót bình luận.
- **Tải triệt để dữ liệu (Deep Scroll & Unfold)**:
  - Tự động cuộn trang theo từng bước `PAGE_DOWN`.
  - Tìm kiếm và kích hoạt toàn bộ các nút mở rộng:
    - `"Xem các bình luận trước"`, `"bình luận khác"`, `"View more comments"`.
    - `"Xem phản hồi"`, `"phản hồi"`, `"View replies"`.
  - Kiểm tra trạng thái dừng: Nếu sau 3 lần cuộn liên tiếp không xuất hiện comment mới thì dừng cuộn.
  - Cuộn ngược lên đầu trang và rà quét lại lần cuối để tránh sót dữ liệu do lazy load.
- **Trích xuất dữ liệu đa trường tinh gọn**:
  - `extract_comment_text`: Thực thi đoạn JavaScript xóa các thẻ `<a>` liên kết profile (tên người được reply trong text) để lấy nội dung comment thuần túy, nhưng vẫn giữ nguyên hashtag và liên kết web khác.
  - `extract_commenter_info`: Trích xuất tên người bình luận và link profile Facebook.
  - `extract_comment_ids`: Tách chính xác `comment_id` (comment gốc) và `reply_comment_id` (nếu là phản hồi).
  - `parse_relative_time`: Quy đổi các mốc thời gian dạng tương đối (ví dụ: *"5 giờ"*, *"2 phút"*, *"1 ngày"*) sang định dạng ngày chuẩn `YYYY-MM-DD`.
  - `is_reply_comment` & `find_parent_comment_id`: Thuật toán phân cấp cha - con dựa trên cấu trúc DOM lồng nhau và độ thụt lề (margin/padding).
- **Xuất bản & Đồng bộ dữ liệu cào**:
  - Xuất ra file CSV độc lập theo chuẩn UTF-8 có BOM (`utf-8-sig`) qua Pandas.
  - Tự động cập nhật vào `post_structure.json` cục bộ.
  - Gọi API gửi dữ liệu cào lên MongoDB CRM (`payloadScrapedComment`, `payloadScrapedReply`).
  - Gửi bản tin socket báo cáo bình luận mới (`comment_byB`, `reply_comment_byB`) và tiến độ (`type: crawl_comment`, trạng thái `started` -> `progress` -> `finished`).
- **Chế độ kích hoạt cào**:
  - Cào theo chu kỳ: `AUTO_CRAWL_INTERVAL = 8000s` cho tối đa `MAX_POSTS_TO_CRAWL = 30` bài mới nhất.
  - Cào theo lệnh thời gian thực từ CRM: `crawl_comment_by_CRM`.
  - Cào thủ công trang hiện tại: `manual_crawl_current_page`.

---

### 3.4. Quản lý tài khoản, đồng bộ MongoDB & Quản trị Proxy
- **Quản lý danh sách tài khoản**:
  - `user_accounts.json` lưu trữ thông tin chi tiết: `user_id_QLC`, `user_id_chat`, `facebook_username`, `facebook_password`, `roleWebSocket`, `nameFb`, `to`, thông tin Proxy.
  - Tự động đọc file cookie riêng biệt theo từng user: `%APPDATA%/fb_cookies_<username>.json`.
- **Cơ chế đồng bộ Facebook ID thực tế vào MongoDB**:
  - Khi mở trình duyệt vào Facebook, tool trích xuất UID thực tế từ mã nguồn trang (`page_source`) qua regex `"userID":"([0-9]+)"`.
  - Gọi hàm `update_device_fb_pc_facebook_id` trong `utils.py`.
  - Kết nối trực tiếp tới MongoDB (`MONGO_URI`, Database `Facebook`, Collection `devices`).
  - Tìm kiếm thiết bị theo `SERVER_NAME` (ví dụ `S08`) và khớp tài khoản tương ứng, sau đó ghi đè giá trị `fb_id` chuẩn vào document.
- **Hạ tầng Proxy**:
  - Hỗ trợ Proxy IP:Port với xác thực tài khoản (HTTP/HTTPS Auth).
  - Các script `check_proxy.py`, `test_all_proxies.py`, `test_proxy.py` cho phép test ping, IP trả về và độ trễ trước khi đưa vào chạy bot.

---

### 3.5. Cơ chế chạy nền, ẩn danh và chống phát hiện (Anti-Detect)
- **Tùy biến Chrome Driver & Flags**:
  - Sử dụng Chrome Profile thực tế đã lưu cache (`--user-data-dir`, `--profile-directory=Default`), bỏ qua các bước xác minh 2FA / Checkpoint phức tạp.
  - Tắt thông báo (`--disable-notifications`), tối ưu bộ nhớ (`--no-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`).
- **Mô phỏng hành vi tự nhiên (Human Emulation)**:
  - Gõ phím từng ký tự ngẫu nhiên (delay 100ms - 400ms).
  - Cuộn trang chia nhỏ tọa độ theo 30 FPS (`smooth_scroll`).
  - Thời gian trễ giữa các hành động mang tính ngẫu nhiên (random uniform từ 2 đến 8 giây).
- **Chạy ngầm trên Windows**:
  - `hide_process()`: Gọi hàm `ShowWindow(hwnd, 0)` để ẩn hoàn toàn cửa sổ Console của tiến trình.
  - Đặt độ ưu tiên hệ thống ở mức `BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW` để tránh chiếm dụng CPU của máy trạm.
  - `kill_existing_process()`: Tự động phát hiện và dọn dẹp các tiến trình `toolfacebook.exe` treo cũ trước khi khởi động phiên mới.

---

## 4. SƠ ĐỒ VÀ PHÂN TÍCH CÁC LUỒNG XỬ LÝ CHÍNH (WORKFLOWS)

### 4.1. Luồng khởi động và vòng lặp hoạt động chính

```mermaid
flowchart TD
    Start([Khởi động Tool]) --> KillOld[Dọn dẹp tiến trình cũ: kill_existing_process]
    KillOld --> InitSystem[Khởi tạo Windows API: hide_process & run_as_trusted]
    InitSystem --> LaunchBrowser[Khởi tạo Selenium Chrome từ User Data Dir]
    LaunchBrowser --> CheckLogin{Đã đăng nhập Facebook?}
    
    CheckLogin -- Chưa --> WaitForLogin[Chờ người dùng đăng nhập thủ công tối đa 300s]
    WaitForLogin --> CheckLogin
    
    CheckLogin -- Đã đăng nhập --> ParseUID[Trích xuất Facebook UID từ Page Source]
    ParseUID --> SyncMongo[Cập nhật fb_id vào MongoDB devices_fb_pc]
    SyncMongo --> StartWS[Khởi chạy tác vụ ngầm: connect_websocket]
    
    StartWS --> MainLoop{Vòng lặp chính While True}
    
    MainLoop --> CheckWSState{WebSocket Task còn chạy?}
    CheckWSState -- Đã dừng --> RestartWS[Tự động restart WebSocket Task]
    RestartWS --> CheckPriority
    CheckWSState -- Đang chạy --> CheckPriority
    
    CheckPriority{Có cờ ngắt stop_browsing & pending_posts?}
    CheckPriority -- CÓ --> ProcessWSCommand[Ưu tiên xử lý lệnh WebSocket ngay lập tức]
    ProcessWSCommand --> MainLoop
    
    CheckPriority -- KHÔNG --> Action1[1. Lướt dạo Newfeed: surf_facebook]
    Action1 --> CheckFlag1{stop_browsing?}
    CheckFlag1 -- Có --> MainLoop
    CheckFlag1 -- Không --> Action2[2. Xem video Facebook Watch: watch_videos]
    
    Action2 --> CheckFlag2{stop_browsing?}
    CheckFlag2 -- Có --> MainLoop
    CheckFlag2 -- Không --> Action3[3. Kiểm tra chu kỳ cào comment tự động]
    
    Action3 --> MainLoop
```

---

### 4.2. Luồng kết nối WebSocket và cơ chế ưu tiên ngắt (Preemptive Interruption)

```mermaid
sequenceDiagram
    autonumber
    participant Server as WebSocket Server (Bên A)
    participant WS as Task connect_websocket()
    participant State as Biến Global (pending_posts, stop_browsing)
    participant Bot as Main Loop Bot (Bên B)

    Bot->>WS: asyncio.create_task(connect_websocket)
    WS->>Server: Kết nối wss://socket.hungha365.com:4000
    WS->>Server: {"type": "register", "clientId": roleWebSocket}
    Server-->>WS: {"type": "register_success"}

    loop Lắng nghe tin nhắn
        Server->>WS: Gửi bản tin (new_post / comment / reply_comment / crawl_comment_by_CRM)
        WS->>WS: Parse JSON & tải ảnh đính kèm (nếu có)
        WS->>State: pending_posts.append(data)
        WS->>State: stop_browsing = True (KÍCH HOẠT CỜ NGẮT)
        Note over State,Bot: Bot đang ở surf_facebook hoặc watch_videos lập tức nhận cờ ngắt và thoát vòng lặp
        Bot->>State: Lấy lệnh đầu tiên trong pending_posts
        Bot->>Bot: Thực thi lệnh tức thời (Đăng bài / Bình luận / Cào data)
        Bot->>Server: Gửi kết quả hoàn thành (URL_post / comment_result / v.v.)
        Bot->>State: stop_browsing = False (Hạ cờ ngắt)
    end

    Note over WS,Server: Nếu mất kết nối: Tự động reconnect sau 5s -> tăng dần tối đa 60s
```

---

### 4.3. Luồng đăng bài viết tự động (Post News Feed)

```mermaid
flowchart TD
    ReceivePost([Nhận lệnh new_post từ WebSocket]) --> DownloadMedia{Có ảnh đính kèm attachments?}
    DownloadMedia -- Có --> FetchImgs[Tải ảnh từ CDN về máy: download_image]
    DownloadMedia -- Không --> OpenHome[Về trang chủ Facebook]
    FetchImgs --> OpenHome
    
    OpenHome --> CheckImages{Có ảnh đã tải về?}
    
    CheckImages -- Có ảnh --> ClickPhotoBtn[Click nút Ảnh/video: photo_button]
    ClickPhotoBtn --> InputFiles[Gửi danh sách file path vào input type='file']
    InputFiles --> WaitRender[Chờ render preview ảnh trong DOM tối đa 10s]
    WaitRender --> FocusText1[Focus vào hộp văn bản p_tag]
    FocusText1 --> TypeContent1[Nhập nội dung bài viết]
    
    CheckImages -- Không ảnh --> ClickCreateBtn[Click nút tạo bài viết: h3_post]
    ClickCreateBtn --> FocusText2[Focus vào hộp văn bản p_tag]
    FocusText2 --> TypeContent2[Nhập nội dung bài viết]
    
    TypeContent1 --> SubmitPost[Tìm nút Đăng / Post]
    TypeContent2 --> SubmitPost
    
    SubmitPost --> RetryClick[Thử click Đăng: Normal -> JS -> ActionChains]
    RetryClick --> WaitPost[Chờ Facebook xử lý đăng bài 4-6s]
    
    WaitPost --> LocateFirstPost[Tìm bài đầu tiên: div aria-posinset='1']
    LocateFirstPost --> FindTimestamp[Tìm thẻ link a chứa __cft__ thứ 3]
    FindTimestamp --> ClickTimestamp[Click timestamp để chuyển URL bài viết chuẩn]
    ClickTimestamp --> GetPostUrl[Lấy current_url của bài viết]
    
    GetPostUrl --> SaveCRM[Gọi API create_post lưu vào CRM Database]
    SaveCRM --> UpdateStruct[Thêm bài viết vào post_structure.json]
    UpdateStruct --> ReplyWS[Gửi URL_post về WebSocket Server]
    ReplyWS --> CloseModal[Đóng popup bài viết]
    CloseModal --> CleanImages[Xóa file ảnh tạm đã tải về]
    CleanImages --> DonePost([Hoàn tất đăng bài])
```

---

### 4.4. Luồng bình luận và trả lời phân cấp (Comment & Reply Hierarchy)

```mermaid
flowchart TD
    StartCmt([Nhận yêu cầu Comment / Reply]) --> ActionType{Loại yêu cầu?}
    
    %% BÌNH LUẬN GỐC
    ActionType -- comment --> NavPostUrl[Điều hướng tới URL bài viết]
    NavPostUrl --> FindCmtBtn[Tìm nút Bình luận loại trừ nút ảnh/media]
    FindCmtBtn --> OpenCmtBox[Mở comment box contenteditable]
    OpenCmtBox --> TypeCmt[Nhập nội dung và nhấn Enter]
    TypeCmt --> WaitCmtDom[Đợi bình luận render trong DOM 3s]
    WaitCmtDom --> ExtractCmtId[Lấy href chứa comment_id cuối cùng]
    ExtractCmtId --> AddCmtStruct[Thêm comment vào post_structure.json]
    AddCmtStruct --> ApiCmt[Gọi API create_comment]
    ApiCmt --> SendWsCmt[Gửi comment_result về WebSocket]
    
    %% TRẢ LỜI BÌNH LUẬN CẤP 1
    ActionType -- reply_comment --> NavReplyUrl[Điều hướng tới URL bài viết]
    NavReplyUrl --> FindCmtContainer[Tìm container x18xomjl xbcz3fp chứa commentId]
    FindCmtContainer --> ClickReplyBtn[Click nút Trả lời bên trong container]
    ClickReplyBtn --> OpenReplyBox[Mở hộp nhập phản hồi]
    OpenReplyBox --> TypeReply[Nhập nội dung phản hồi và Enter]
    TypeReply --> WaitReplyDom[Đợi phản hồi render]
    WaitReplyDom --> ExtractReplyId[Lấy reply_comment_id từ link timestamp]
    ExtractReplyId --> AddReplyStruct[Lưu reply vào nhánh replies của comment trong structure]
    AddReplyStruct --> ApiReply[Gọi API create_reply_comment]
    ApiReply --> SendWsReply[Gửi reply_to_comment_result về WebSocket]

    SendWsCmt --> Finish([Kết thúc])
    SendWsReply --> Finish
```

---

### 4.5. Luồng cào comment tự động và đồng bộ cấu trúc Post

```mermaid
flowchart TD
    TriggerCrawl([Kích hoạt Cào Comment: Chu kỳ / CRM / Thủ công]) --> LoadStruct[Đọc post_structure.json]
    LoadStruct --> SortPosts[Sắp xếp bài viết theo thời gian tạo mới nhất]
    SortPosts --> SlicePosts[Lấy tối đa MAX_POSTS_TO_CRAWL = 30 bài]
    
    SlicePosts --> LoopPost{Lặp qua từng bài viết}
    
    LoopPost -- Xong tất cả --> WSFinish[Gửi trạng thái finished qua WebSocket]
    WSFinish --> EndCrawl([Kết thúc cào])
    
    LoopPost -- Bài tiếp theo --> NavPost[Mở URL bài viết trên trình duyệt]
    NavPost --> SetFilter[Chuyển bộ lọc sang 'Tất cả bình luận']
    SetFilter --> ScrollExpand[Cuộn sâu và click toàn bộ nút Xem thêm / Xem phản hồi]
    ScrollExpand --> FindArticles[Tìm tất cả phần tử div role='article']
    
    FindArticles --> LoopElement{Trích xuất từng comment}
    
    LoopElement --> CleanText[Dùng JavaScript lọc bỏ tag profile lấy text thuần]
    CleanText --> ExtractMeta[Trích xuất Commenter, Link, Time, IDs]
    ExtractMeta --> Classify{Có reply_comment_id?}
    
    Classify -- KHÔNG --> RootList[Xếp vào danh sách Comment gốc]
    Classify -- CÓ --> ReplyList[Xếp vào danh sách Reply con]
    
    RootList --> SaveRoots[Bước 1: Lưu toàn bộ Comment gốc vào Structure & CRM Database]
    SaveRoots --> SaveReplies[Bước 2: Ghép toàn bộ Reply vào Comment cha tương ứng]
    
    SaveReplies --> PushWS[Gửi thông báo comment_byB & reply_comment_byB lên WebSocket]
    PushWS --> ClosePopup[Đóng popup bài viết bằng ESC / nút Đóng]
    ClosePopup --> LoopPost
```

---

## 5. CẤU TRÚC DỮ LIỆU VÀ CÁC ĐỊNH DẠNG PROTOCOL

### 5.1. Cấu trúc Post Structure (`post_structure.json`)
File này lưu vết toàn bộ bài viết, các bình luận và phản hồi phân cấp theo cây nhị phân/quan hệ cha con:

```json
{
  "posts": {
    "1755231400222": {
      "url": "https://www.facebook.com/permalink.php?story_fbid=pfbid...&id=61558260513431",
      "post_id": "1755231400222",
      "database_post_id": "689eb4dd65ecb2391464bbf6",
      "created_at": "2025-08-15T11:17:33.361477",
      "comments": {
        "1029384756": {
          "comment_fb_id": "1029384756",
          "content": "Công việc này còn tuyển không ạ?",
          "commenter_name": "Nguyễn Văn A",
          "commenter_link": "https://www.facebook.com/user/1000123456",
          "comment_date": "2025-08-15",
          "link_comment": "https://www.facebook.com/permalink.php?story_fbid=...&comment_id=1029384756",
          "created_at": "2025-08-15T11:20:00.000000",
          "scraped_at": "2025-08-15T11:25:00.000000",
          "replies": {
            "1029384799": {
              "reply_fb_id": "1029384799",
              "content": "Dạ bên mình vẫn đang nhận hồ sơ bạn nhé!",
              "commenter_name": "Hải Linh Timviec365",
              "commenter_link": "https://www.facebook.com/user/1000987654",
              "comment_date": "2025-08-15",
              "link_comment": "https://www.facebook.com/permalink.php?story_fbid=...&comment_id=1029384756&reply_comment_id=1029384799",
              "created_at": "2025-08-15T11:22:00.000000",
              "scraped_at": "2025-08-15T11:25:00.000000"
            }
          }
        }
      }
    }
  }
}
```

---

### 5.2. Cấu trúc User Accounts (`user_accounts.json`)

```json
[
  {
    "user_id_QLC": "22616984",
    "user_id_chat": "10407256",
    "facebook_username": "account_email@gmail.com",
    "facebook_password": "password_secret",
    "facebook_2fa_code": "",
    "note": "Tài khoản Hải Linh",
    "roleWebSocket": "B22616984",
    "nameFb": "Hải Linh",
    "to": "22773024",
    "proxy_ip": "42.118.161.103",
    "proxy_port": "35270",
    "proxy_user": "muaproxy689ef8202bc87",
    "proxy_pass": "lyl1nqbxq4ghgpyu"
  }
]
```

---

### 5.3. Giao thức WebSocket Payload

| Bản tin (Type) | Hướng truyền | Mục đích | Các trường dữ liệu cốt lõi |
|---|---|---|---|
| `register` | Client -> Server | Khởi tạo phiên định danh cho bot | `type`, `clientId` (ví dụ `B22616984`) |
| `register_success` | Server -> Client | Xác nhận đăng ký socket thành công | `type` |
| `new_post` | Server -> Client | Chỉ thị đăng bài viết mới | `type`, `content`, `postId`, `authorId`, `attachments` (danh sách URL ảnh) |
| `URL_post` | Client -> Server | Báo cáo link bài viết sau khi đăng | `type`, `URL`, `authorName`, `postId`, `to`, `timestamp` |
| `comment` | Server -> Client | Yêu cầu bình luận vào post | `type`, `URL`, `content`, `postId`, `authorId` |
| `comment_result` | Client -> Server | Báo cáo kết quả bình luận | `type`, `status`, `URL`, `content`, `comment_id`, `postId`, `to` |
| `reply_comment` | Server -> Client | Yêu cầu trả lời comment cấp 1 | `type`, `URL`, `content`, `commentId`, `postId`, `authorId` |
| `reply_to_comment_result` | Client -> Server | Báo cáo kết quả trả lời comment | `type`, `status`, `URL`, `commentId`, `replyId`, `postId`, `to` |
| `reply_reply_comment` | Server -> Client | Yêu cầu trả lời reply cấp 2 | `type`, `URL`, `content`, `commentId`, `replyId`, `postId` |
| `crawl_comment_by_CRM` | Server -> Client | Lệnh cào comment tức thời | `type`, `facebookId`, `authorId` |
| `crawl_comment` | Client -> Server | Báo cáo trạng thái tiến trình cào | `type`, `status` (`started`/`progress`/`finished`/`error`), `message`, `postCount`, `currentPost`, `to` |
| `comment_byB` | Client -> Server | Đẩy comment cào được lên server | `type`, `postId`, `content`, `authorId`, `commentFbId`, `linkUserComment`, `to` |
| `reply_comment_byB` | Client -> Server | Đẩy reply cào được lên server | `type`, `postId`, `commentId`, `replyId`, `content`, `authorId`, `linkUserReply`, `to` |
| `friend_request_status` | Client -> Server | Báo cáo hạn mức kết bạn trong ngày | `type`, `status` (`count`, `max`, `remaining`, `date`), `to` |

---

### 5.4. Cấu hình môi trường (`.env`)

```ini
SERVER_NAME="S08"
CHROME_USER_DATA_DIR=r"C:\Nuoi_FB\chrome_profile\Hai_Linh_0936212850_timviec365@"
CHROME_USER_DATA_DIR_1=r"C:\Nuoi_FB\chrome_profile\Hoang_Maii_0966338017_Timviec365@"
CHROME_USER_DATA_DIR_2=r"C:\Nuoi_FB\chrome_profile\My_Vu_Hoang_myv217704@gmail.com_Timviec365@@"
CHROME_USER_DATA_DIR_3=r"C:\Nuoi_FB\chrome_profile\Hai_Linh_0936212850_timviec365@"
MONGO_URI=mongodb://myuser_duc:Anhduc14062002%40%23%24@123.24.206.25:27017/?authSource=admin
MONGO_DB=Facebook
MONGO_COLLECTION_DEVICES=devices
```

---

## 6. ĐÁNH GIÁ KỸ THUẬT, ĐIỂM MẠNH VÀ KHUYẾN NGHỊ TỐI ƯU

### Điểm mạnh nổi bật:
1. **Kiến trúc phản ứng nhanh (Reactive Preemption)**: Nhờ biến cờ `stop_browsing`, bot không bị kẹt trong các chu kỳ lướt newsfeed hay xem video dài mà có thể ngắt ngay khi có việc gấp từ WebSocket (đăng bài, bình luận).
2. **Xử lý DOM Facebook hiện đại và chi tiết**:
   - Sử dụng các kỹ thuật định vị nâng cao bằng kết hợp `aria-label`, quan hệ tổ tiên `ancestor::`, và cấu trúc CSS module mã hóa của Facebook.
   - Tránh triệt để việc click nhầm vào ảnh đính kèm khi tìm nút Comment.
   - Tìm chính xác container comment và reply lồng nhau.
3. **Cơ chế chống phát hiện (Anti-Ban)**:
   - Mô phỏng tốc độ gõ phím ngẫu nhiên.
   - Cuộn trang chia nhỏ 30 FPS.
   - Tắt âm thanh media để không gây chú ý hoặc tiêu tốn tài nguyên.
   - Giới hạn nghiêm ngặt 10 lượt kết bạn/ngày.
4. **Đồng bộ cơ sở dữ liệu thời gian thực**:
   - Tự động lấy Facebook ID chính xác từ `page_source` và ghi nhận trực tiếp vào MongoDB `devices_fb_pc`.
   - Lưu trữ song song cả ở CRM API lẫn file JSON cục bộ để chống mất mát dữ liệu khi mất mạng.

### Các lưu ý và khuyến nghị cải tiến:
1. **Tránh nhân bản mã nguồn (`toolfacebook1.py`, `2.py`, `3.py`)**:
   - Hiện tại dự án copy toàn bộ file 4.900 dòng thành 4 file chỉ để đổi biến `CHROME_USER_DATA_DIR`.
   - *Khuyến nghị*: Chuyển sang nhận tham số dòng lệnh: `python toolfacebook.py --instance 1` hoặc `python toolfacebook.py --profile "C:\..."`.
2. **Kiểm tra trạng thái kích hoạt của các tính năng**:
   - Trong `toolfacebook.py` dòng 4956 - 4970 và 5015 - 5085, một số khối tính năng đang có tiền tố `elif False and ...` hoặc bị comment `#` (như `post_news_feed` ở dòng 4957, `comment_on_post_url`, `reply_to_comment`).
   - Cần cấu hình linh hoạt qua biến môi trường hoặc cấu hình JSON thay vì hardcode `False and` trực tiếp trong code khi đưa vào chạy sản xuất.
3. **Thống nhất Backend API Endpoint**:
   - Dự án đang đồng thời gọi `api.py` (`https://socket.hungha365.com:4000/api`) và `toolfacebook_lib.py` (`http://192.168.0.123:5000/`). Nên tập trung hóa cấu hình URL của hai hệ thống này vào file `.env`.
4. **Tối ưu hóa quản lý tiến trình Chrome**:
   - Thêm cơ chế tự động giải phóng bộ nhớ (clear browser cache định kỳ) nếu tool chạy liên tục nhiều ngày trên máy trạm để tránh tràn RAM.

---
*Báo cáo được hoàn thành và cập nhật tự động vào hệ thống tài liệu dự án.*
