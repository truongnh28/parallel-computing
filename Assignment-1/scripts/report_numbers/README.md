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
