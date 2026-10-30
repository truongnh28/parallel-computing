# scripts/report_numbers

Các script tính những con số trong báo cáo mà bảng do `scripts/plot.py` sinh ra không in sẵn
(tỉ số, độ chậm, độ công bằng, số lần CAS hỏng trên mỗi lần tăng...). Mỗi con số như vậy trong
`report/sections/*.tex` có một comment `% nguồn:` chỉ tới script tương ứng ở đây.

Chạy từ bất kỳ thư mục nào, cần numpy và matplotlib (các script `rlock_*` nạp `scripts/plot.py`):

    python3 scripts/report_numbers/rlock_analyze.py
    python3 scripts/report_numbers/rbc_analyze.py

- `rlock_*`: mục Kết quả, mutex lock (RQ2, RQ3).
- `rbc_*`: mục Kết quả, barrier và counter (RQ4, RQ5).
- `corr_*`: mục Kiểm tra tính đúng đắn, Phương pháp đo, Các yếu tố đe dọa độ tin cậy.
- `extra_analyze.py`: thí nghiệm phụ trên apple-m5 (`experiments/run_extra.py`, kết quả ở
  `results/apple-m5/extra/`): khoảng trống trao lock của MCS (RQ2) và lần đo lại khi tải thấp
  (mục Các yếu tố đe dọa độ tin cậy). Script này chỉ cần thư viện chuẩn của Python.
- `rcross_*.py`: RQ5 và RQ6 (so sánh ba máy), các tỉ số tính thẳng từ `results/<máy>/*.csv`;
  chỉ cần thư viện chuẩn của Python.
