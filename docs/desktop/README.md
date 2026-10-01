# Threads Desktop v1 — Tổng quan kế hoạch

**Trạng thái:** bản kế hoạch đề xuất trên nhánh riêng; **chưa triển khai ứng dụng, chưa nghiệm thu production**. Phiên mới **bắt đầu tại [SESSION_HANDOFF.md](SESSION_HANDOFF.md)** và đọc [bản audit](PREIMPLEMENTATION_AUDIT.md) trước khi viết code.

## Mục tiêu sản phẩm đã thống nhất

Một installer Windows và một ứng dụng **Threads Desktop** (Tauri 2 + Rust + React/TypeScript). Lần đầu chọn **Controller**, **Worker** hoặc **Console**; role Controller/Worker gần như cố định cho đến khi decommission/reset. Bấm **X** chỉ ẩn UI xuống system tray; backend/Worker tiếp tục chạy. Chọn **Quit** mới xác nhận dừng node an toàn và thoát. Node tự khởi động khi Windows user chuyên dụng đăng nhập, **không** chạy qua logout hoặc sau reboot khi chưa login.

- **Controller:** quản lý một Workspace duy nhất, PostgreSQL + FastAPI + scheduler; Windows khách hàng không cần tự cài Python/PostgreSQL/Docker.
- **Worker:** sử dụng lại Python Worker, device key bảo vệ bằng DPAPI, browser Chromium headed và profile riêng; operator đăng nhập trực tiếp trên Worker UI để thêm Threads account và làm human-assisted login.
- **Console:** máy quản trị từ xa, chỉ kết nối HTTPS và đăng nhập bằng Operator account; không chạy runtime business local.
- **Bảo mật:** LAN stable IP trước, xác minh Controller fingerprint trước khi gửi mã pairing hoặc mật khẩu, không bypass login trên máy Controller, RBAC bốn vai trò. Browser cookies/password/profile không rời Worker.
- **Tạm hoãn:** backup/recovery, email khôi phục Owner, Internet, automatic update, Windows Service chạy trước login, SSO/MFA, auto-discovery, HA. Việc chưa có backup là **rủi ro mất dữ liệu**, không được gọi bản demo là production-ready.

## Tài liệu thực thi

1. [ADR-0007 — kiến trúc một app Windows-first](../adr/0007-windows-first-single-app-desktop.md): quyết định đã chốt và ranh giới thành phần.
2. [DELIVERY_PLAN.md](DELIVERY_PLAN.md): **14 work packages DX-01…DX-14**, phụ thuộc, deliverables, critical path và definition of done.
3. [SECURITY_AND_PROTOCOLS.md](SECURITY_AND_PROTOCOLS.md): Windows/Controller/Worker/Operator identity, HTTPS fingerprint verification, one-time pairing, RBAC, Worker-centric browser onboarding và data boundary.
4. [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md): tiêu chí nghiệm thu, kiểm thử tiêu cực, regression, ma trận Windows/Linux/CI và ba máy.
5. [WINDOWS_REHEARSAL.md](WINDOWS_REHEARSAL.md): first-run, data root, process startup/shutdown, reboot, packaging, runbook và checklist bằng chứng.
6. [ISSUE_MAP.md](ISSUE_MAP.md): mapping epic #94 và 14 issue #95–#108, thứ tự, phụ thuộc và hard gates.
7. [PREIMPLEMENTATION_AUDIT.md](PREIMPLEMENTATION_AUDIT.md): 18 phát hiện và việc cần sửa trước khi nghiệm thu implementation.
8. [SESSION_HANDOFF.md](SESSION_HANDOFF.md): tài liệu **bàn giao một phiên mới** — nguồn sự thật, quyết định đã chốt, blocker và bước kế tiếp.

## Thứ tự ưu tiên

- **M1:** chứng minh **engineering test bundle/prototype chỉ bind loopback với dữ liệu giả** có thể khởi động Controller trên Windows sạch; đóng UI vẫn chạy; Quit và mở lại giữ nguyên PostgreSQL state. Installer cuối và first OWNER + HTTPS thuộc các giai đoạn sau.
- **M2:** Operator RBAC + LAN HTTPS trust, Worker pairing + Console login.
- **M3:** Worker thêm account và đăng nhập Threads tại máy của mình; Controller tạo Account/assignment atomically; re-login/reassign an toàn.
- **M4:** UI Controller/Worker/Console, một installer nội bộ và diễn tập E2E ba Windows PC/VM; độc lập với production release gate.

Đừng triển khai tất cả issue cùng lúc. Chỉ bắt đầu implementation issue sau khi phạm vi được coordinator cho phép; mỗi PR phải có AC, test evidence trên exact head SHA và nghiệm thu độc lập trước merge.

## Bước cần review trước khi viết code

Duyệt ADR-0007, DX-01 và policy RBAC từng hành động (ma trận trong DELIVERY_PLAN.md). Nghiệm thu DX-03 **feasibility packaging** trước khi coi `threads-runtime.exe` là lựa chọn cuối cùng; duyệt security protocol DX-06/08 trước khi viết TLS pairing. Các external release gate cũ #3, #80, #62, #11 và #1 vẫn độc lập.
