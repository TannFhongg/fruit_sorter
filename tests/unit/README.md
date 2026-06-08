# Hướng Dẫn Sử Dụng Unit Tests

Thư mục `tests/unit` chứa các unit test cho những phần lõi của hệ thống phân loại trái cây: nhận diện, điều khiển sort, serial, event bus, database, Flask API và kiểm tra timing. Các test này không yêu cầu phần cứng thật vì dùng mock, fake object, queue trong bộ nhớ hoặc database tạm.

Chạy lệnh từ thư mục gốc repo:

```bash
cd /home/nhatan/fruit_sorter
```

## Cài Đặt Và Môi Trường

Repo đã có `venv`. Nếu cần cài lại dependency:

```bash
./venv/bin/pip install -r requirements.txt
```

Khi chạy pytest, cần đặt `PYTHONPATH=.` để Python import được các package top-level như `control`, `database`, `drivers`, `perception`, `shared` và `web`.

Chạy toàn bộ unit test:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/unit -q
```

Chạy chi tiết hơn:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/unit -v
```

Chạy một file cụ thể:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/unit/<file>.py -q
```

Ví dụ:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/unit/test_sort_controller.py -q
```

## Danh Sách File Test

| File | Module chính | Nội dung kiểm tra | Lệnh chạy riêng |
| --- | --- | --- | --- |
| `test_timing_validator.py` | `control.timing_validator` | Kiểm tra cửa sổ thời gian IR1/IR2, biên hợp lệ, sensor không xác định và hàm `compute_window`. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_timing_validator.py -q` |
| `test_sort_controller.py` | `control.sort_controller` | Kiểm tra FIFO detection queue, timing gate, gửi lệnh servo sweep, mismatch sensor-servo, DB queue, event dashboard và purge detection quá hạn. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_sort_controller.py -q` |
| `test_db_writer.py` | `database.db_writer` | Kiểm tra ghi batch `SortEvent` vào SQLite và cập nhật `daily_stats` theo ngày của event, kể cả batch qua nửa đêm. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_db_writer.py -q` |
| `test_serial_link.py` | `drivers.serial_link` | Kiểm tra xử lý `PONG`, đưa `IR_TRIGGER` vào queue, heartbeat chỉ gửi ping và logic reconnect theo cấu hình. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_serial_link.py -q` |
| `test_event_bus.py` | `shared.event_bus` | Kiểm tra subscribe, emit, unsubscribe, idempotent subscription, clear, exception isolation, thread safety, singleton và hằng số `EVT_*`. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_event_bus.py -q` |
| `test_flask_app.py` | `web.flask_app` | Kiểm tra `/api/health`, payload `sort_event`, thống kê live có rejects và lỗi truy vấn database của `/api/stats/today`. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_flask_app.py -q` |
| `test_fruit_detector.py` | `perception.fruit_detector` | Kiểm tra fail-closed khi model production lỗi, simulation mode rõ ràng, claim một object một lần và sắp xếp detection downstream-first. | `PYTHONPATH=. ./venv/bin/pytest tests/unit/test_fruit_detector.py -q` |

## Cách Dùng Khi Phát Triển

Khi sửa một module, chạy file test gần nhất trước, sau đó chạy lại toàn bộ thư mục:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/unit/test_<module>.py -q
PYTHONPATH=. ./venv/bin/pytest tests/unit -q
```

Các test trong thư mục này chủ yếu kiểm tra hành vi ở mức unit:

- `MagicMock`, fake serial và fake socket được dùng để tránh phụ thuộc Arduino hoặc SocketIO thật.
- `tmp_path` được dùng cho database tạm, không ghi vào database production.
- `deque`, `threading.Event` và lock thật được dùng để mô phỏng queue/runtime state.
- Các test có thể gọi method private như `_handle_ir_trigger`, `_run_inference` hoặc `_write_batch` để khóa hành vi quan trọng của từng module.

## Lưu Ý

- `__pycache__` là cache Python sinh tự động, không phải file test cần chạy hoặc tài liệu hóa.
- Nếu chạy thiếu `PYTHONPATH=.`, pytest có thể báo `ModuleNotFoundError` cho các package trong repo.
- Nếu thêm file test mới trong `tests/unit`, cập nhật README này với mục đích test và lệnh chạy riêng.
