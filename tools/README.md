# Hướng Dẫn Sử Dụng Tools

Thư mục `tools` chứa các script chạy thủ công để kiểm tra phần cứng, kiểm tra model và hiệu chỉnh timing cho hệ thống phân loại trái cây. Các script này nên được chạy từ thư mục gốc repo để các đường dẫn mặc định như `config/hardware_config.yaml`, `logs/test_frame.jpg` và `models/best_ncnn_model` hoạt động đúng.

```bash
cd /home/nhatan/fruit_sorter
```

## Cài Đặt Và Môi Trường

Repo đã có `venv`. Nếu cần cài lại dependency:

```bash
./venv/bin/pip install -r requirements.txt
```

Khi chạy tool, dùng Python trong `venv`:

```bash
./venv/bin/python tools/<file>.py
```

Một số tool yêu cầu phần cứng thật:

- Arduino nối qua UART/USB serial cho `test_serial.py` và `calibrate_belt.py`.
- USB camera cho `test_camera.py`.
- File NCNN model trong `models/best_ncnn_model` và ảnh đầu vào cho `test_model.py`.
- Băng chuyền và cảm biến IR thật cho `calibrate_belt.py`.

## Thứ Tự Kiểm Tra Khuyến Nghị

Chạy theo thứ tự này khi setup hoặc debug hệ thống:

```bash
./venv/bin/python tools/test_serial.py --port /dev/ttyUSB0
./venv/bin/python tools/test_camera.py --device 0 --frames 30
./venv/bin/python tools/test_model.py --image logs/test_frame.jpg
./venv/bin/python tools/calibrate_belt.py --runs 10 --config config/hardware_config.yaml --sensors 1 2
```

Ý nghĩa thứ tự:

1. `test_serial.py`: xác nhận Raspberry Pi giao tiếp được với Arduino.
2. `test_camera.py`: xác nhận camera mở được, đọc frame ổn định và lưu ảnh mẫu.
3. `test_model.py`: xác nhận model NCNN load được và inference chạy trên ảnh mẫu.
4. `calibrate_belt.py`: đo timing thực tế sau khi serial, camera, model và cơ khí đã sẵn sàng.

## Danh Sách Tool

| File | Mục đích | Lệnh chạy nhanh | Tác dụng phụ |
| --- | --- | --- | --- |
| `test_serial.py` | Kiểm tra UART với Arduino, gửi `PING`, `STATUS`, `RESET` và in phản hồi. | `./venv/bin/python tools/test_serial.py` | Gửi lệnh `RESET` tới Arduino. |
| `test_camera.py` | Kiểm tra USB camera, đọc frame, đo FPS thực tế và lưu một ảnh kiểm tra. | `./venv/bin/python tools/test_camera.py` | Có thể tạo hoặc thay `logs/test_frame.jpg`. |
| `test_model.py` | Kiểm tra NCNN model với một ảnh tĩnh, xác nhận blob name và đo inference time. | `./venv/bin/python tools/test_model.py --image logs/test_frame.jpg` | Không sửa file. |
| `calibrate_belt.py` | Đo thời gian vật thể đi từ Camera tới IR1/IR2 và đề xuất timing window. | `./venv/bin/python tools/calibrate_belt.py` | Chỉ ghi `config/hardware_config.yaml` nếu xác nhận `y`. |

## `test_serial.py`

Tool này kiểm tra kết nối UART với Arduino Slave bằng cách gửi lần lượt `PING`, `STATUS`, `RESET`, sau đó gửi lại `PING` để xác nhận Arduino phản hồi sau reset.

Chạy mặc định:

```bash
./venv/bin/python tools/test_serial.py
```

Chạy với port khác:

```bash
./venv/bin/python tools/test_serial.py --port /dev/ttyACM0 --baud 115200 --timeout 2.0
```

Tham số:

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--port` | `/dev/ttyUSB0` | Thiết bị serial của Arduino. Kiểm tra bằng `ls /dev/ttyUSB*` hoặc `ls /dev/ttyACM*`. |
| `--baud` | `115200` | Baudrate UART. Cần khớp với firmware Arduino. |
| `--timeout` | `2.0` | Số giây chờ phản hồi cho mỗi lệnh. |

Yêu cầu:

- Arduino đã cắm vào máy chạy repo.
- Firmware Arduino hiểu các lệnh `PING`, `STATUS` và `RESET`.
- Dependency `pyserial` đã được cài từ `requirements.txt`.

Kết quả mong đợi:

- Console in phản hồi cho `PING`, `STATUS`, `RESET`, `PING`.
- Kết thúc bằng `Test hoàn tất`.

Lỗi thường gặp:

- `pip install pyserial`: chưa cài dependency.
- `Lỗi: ... Permission denied`: user hiện tại chưa có quyền đọc/ghi serial port.
- `timeout`: sai port, sai baudrate, Arduino chưa chạy firmware đúng hoặc dây USB/nguồn có vấn đề.

Lưu ý: tool này gửi lệnh `RESET` tới Arduino. Không chạy khi đang vận hành thật nếu reset Arduino có thể làm gián đoạn băng chuyền hoặc servo.

## `test_camera.py`

Tool này mở USB camera bằng OpenCV, cấu hình 640x480 @ 30 FPS, đọc một số frame để đo FPS thực tế và lưu frame đầu tiên đọc thành công.

Chạy mặc định:

```bash
./venv/bin/python tools/test_camera.py
```

Chạy với camera hoặc số frame khác:

```bash
./venv/bin/python tools/test_camera.py --device 0 --frames 60 --save logs/test_frame.jpg
```

Tham số:

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--device` | `0` | Camera index OpenCV, tương ứng `/dev/video0`. |
| `--frames` | `30` | Số frame dùng để đo FPS thực tế. |
| `--save` | `logs/test_frame.jpg` | Đường dẫn lưu frame đầu tiên đọc thành công. |

Yêu cầu:

- USB camera đã kết nối và được hệ điều hành nhận diện.
- Dependency `opencv-python-headless` đã được cài từ `requirements.txt`.

Kết quả mong đợi:

- Console in độ phân giải, FPS camera báo về, số frame đọc thành công và FPS thực tế.
- File ảnh được lưu tại đường dẫn `--save`, mặc định là `logs/test_frame.jpg`.
- Nếu FPS thực tế từ 20 trở lên, tool in `Camera: OK`.

Lỗi thường gặp:

- `Không mở được camera`: sai `--device`, camera đang bị process khác giữ, hoặc hệ điều hành chưa nhận camera.
- FPS thấp hơn 20: kiểm tra USB bandwidth, cổng USB, ánh sáng hoặc cấu hình camera.
- Không thấy file ảnh: kiểm tra quyền ghi thư mục `logs` hoặc chạy lại với `--save` tới đường dẫn khác.

Lưu ý: tool này có thể tạo mới hoặc ghi đè file ảnh ở đường dẫn `--save`.

## `test_model.py`

Tool này load NCNN model từ cấu hình, chạy inference trên một ảnh tĩnh và đo thời gian inference trung bình. Script này không cần camera thật.

Chạy với ảnh mẫu từ `test_camera.py`:

```bash
./venv/bin/python tools/test_model.py --image logs/test_frame.jpg
```

Chạy với config khác:

```bash
./venv/bin/python tools/test_model.py --image logs/test_frame.jpg --config config/hardware_config.yaml
```

Tham số:

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--image` | `logs/test_frame.jpg` | Ảnh tĩnh dùng để kiểm tra inference. |
| `--config` | `config/hardware_config.yaml` | File cấu hình chứa đường dẫn model, input size, labels, thresholds và số thread. |

Yêu cầu:

- Ảnh đầu vào tồn tại. Có thể tạo bằng `test_camera.py`.
- `config/hardware_config.yaml` có mục `model.path`, mặc định trỏ tới `models/best_ncnn_model`.
- Trong thư mục model có `model.ncnn.param` và `model.ncnn.bin`.
- Dependency `ncnn`, `opencv-python-headless` và `numpy` đã được cài từ `requirements.txt`.

Kết quả mong đợi:

- Console in `Model loaded OK`.
- Tool thử các cặp blob name phổ biến và in cặp đúng nếu extract thành công.
- Console in inference time trung bình sau 10 lần chạy.
- Nếu inference dưới 100ms, tool in `Model speed: OK`.

Lỗi thường gặp:

- `Ảnh không tồn tại`: chạy `test_camera.py` trước hoặc truyền đúng `--image`.
- Lỗi load model: kiểm tra `model.path` trong config và hai file `model.ncnn.param`, `model.ncnn.bin`.
- Inference chậm: thử điều chỉnh `model.num_threads` hoặc `model.input_size` trong config.

Lưu ý: script này dùng preprocessing tương ứng với runtime detector, gồm letterbox, chuyển BGR sang RGB và normalize về `[0, 1]`. Nếu sửa preprocessing production, cần giữ `test_model.py` đồng bộ.

## `calibrate_belt.py`

Tool này đo thời gian thực tế từ lúc người vận hành đặt vật thể tại vị trí camera tới khi IR sensor phát hiện vật thể. Kết quả được dùng để đề xuất `ir1_window_ms` và `ir2_window_ms` trong `conveyor.timing`.

Chạy mặc định:

```bash
./venv/bin/python tools/calibrate_belt.py
```

Chạy với số lần đo, config và sensor cụ thể:

```bash
./venv/bin/python tools/calibrate_belt.py --runs 15 --config config/hardware_config.yaml --sensors 1 2
```

Chỉ đo IR1:

```bash
./venv/bin/python tools/calibrate_belt.py --runs 10 --sensors 1
```

Tham số:

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--runs` | `10` | Số lần đo cho mỗi sensor. |
| `--config` | `config/hardware_config.yaml` | File cấu hình dùng để mở serial và có thể ghi timing window. |
| `--sensors` | `1 2` | Danh sách IR sensor cần đo, ví dụ `--sensors 1` hoặc `--sensors 1 2`. |

Yêu cầu:

- Arduino đã kết nối đúng serial port trong `config/hardware_config.yaml`.
- Arduino gửi được event IR trigger theo serial protocol của repo.
- Băng chuyền, cảm biến IR1/IR2 và vật thể thử nghiệm đã sẵn sàng.
- Dependency `pyyaml` và `pyserial` đã được cài từ `requirements.txt`.

Cách chạy thực tế:

1. Đảm bảo serial port trong `config/hardware_config.yaml` đúng với Arduino.
2. Chạy tool và chờ thông báo kết nối Arduino `OK`.
3. Với từng lần đo, nhấn `ENTER`, chờ tool xả tín hiệu serial cũ, rồi đặt vật thể ngay trước camera khi thấy `GO`.
4. Chờ IR trigger tự động. Nếu quá 12 giây không thấy trigger, lần đo đó timeout và cần thử lại.
5. Sau khi đủ số lần đo, đọc trung bình, độ lệch chuẩn và cửa sổ timing đề xuất.
6. Khi tool hỏi `Ghi kết quả vào hardware_config.yaml? [y/N]:`, nhập `y` nếu muốn ghi vào config.

Kết quả mong đợi:

- Console in `delta_t` cho từng lần đo hợp lệ.
- Console in trung bình, độ lệch chuẩn và cửa sổ đề xuất cho từng IR sensor.
- Nếu xác nhận `y`, `conveyor.timing.ir1_window_ms` hoặc `conveyor.timing.ir2_window_ms` được ghi vào `config/hardware_config.yaml`.

Lỗi thường gặp:

- `Kết nối Arduino... THẤT BẠI`: kiểm tra serial port, baudrate, cáp USB và firmware Arduino.
- Timeout 12 giây: vật thể chưa đi tới sensor, sensor không trigger, băng chuyền chưa chạy hoặc chọn sai `--sensors`.
- Timing dao động lớn: đặt vật thể chưa nhất quán, tốc độ băng chuyền chưa ổn định hoặc cảm biến bị nhiễu.
- Kết quả gần 0ms: kiểm tra xem tay hoặc vật thể có còn che cảm biến sau khi nhấn `ENTER` không. Tool đã xả queue trước khi in `GO`, nên trigger phát sinh trước đó sẽ bị bỏ qua.

Lưu ý: tool này chỉ ghi `config/hardware_config.yaml` khi người dùng nhập chính xác `y`. Nếu chọn mặc định `N`, kết quả chỉ được in ra console.

## Lưu Ý Chung

- Các tool trong thư mục này là script thao tác thủ công, không phải unit test. Unit test nằm trong `tests/unit`.
- Không chạy tool phần cứng khi hệ thống đang vận hành thật nếu lệnh kiểm tra có thể reset Arduino, ghi đè ảnh mẫu hoặc thay đổi timing config.
- Nếu thêm script mới vào `tools`, cập nhật README này với mục đích, lệnh chạy, tham số, yêu cầu và tác dụng phụ của script đó.
