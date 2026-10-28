# Assignment 1 — Mutex lock, sense-reversing barrier, concurrent counter

Bài tập lớn 1 môn Parallel Computing (HCMUT): cài đặt bằng C++20

1. **mutex lock**: TAS, TTAS (có và không có exponential backoff), MCS, so với lock thư viện;
2. **sense-reversing barrier** với nhiều cách chờ, so với barrier thư viện;
3. **concurrent counter** bằng lock, compare-and-swap và fetch-and-add;

kèm bộ kiểm tra tính đúng đắn (stress test, ThreadSanitizer, mutation testing) và nghiên cứu hiệu năng trên từng máy của nhóm. Phần 4 của đề (bản phân tán bằng MPI-3 RMA, không bắt buộc) không được thực hiện.

Mọi con số trong báo cáo đi theo một đường duy nhất, không có bước chép tay nào:

```
build/bench_*  →  results/<máy>/*.csv  →  report/figures/<máy>/*.pdf + tables.tex  →  report/report.pdf
   (make)            (make bench)                  (make figures)                       (make report)
```

## Cần đọc gì để hiểu trọn bài

Thứ tự đề nghị. Không cần đọc hết bài 03b và 04, chỉ các mục ghi trong bảng.

**Bắt buộc**

| Bài | Mục | Dùng cho |
|---|---|---|
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/01 - Memory Consistency Models|01 — Memory Consistency]] | cache coherence, store buffer, litmus SB (mục 8), ký hiệu `P\|\|S` (mục 9) | cả ba phần: vì sao lock cần acquire/release, vì sao một cache line ghi chung thành điểm tuần tự |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/02a - Atomic Operations (Part 1)|02a — Atomic (Part 1)]] | data race và race condition (3), lock-free-ness (6), `fetch_add` → `ldadd` (7), CAS, weak/strong, LL/SC (8), false sharing (14.6) | **phần 3** gần như toàn bộ |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/02b - Atomic Operations (Part 2)|02b — Atomic (Part 2)]] | happens-before, acquire/release, `seq_cst` | memory order của lock và barrier |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/03a - Spinlocks|03a — Spinlocks]] | 2 (định nghĩa "đúng"), 6 (MESI), **7 (TAS, TTAS)**, **9 (MCS)**, 11 (kiểm tra đúng đắn) | **phần 1** |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/03b - More on Spinlocks|03b — More on Spinlocks]] | **2** (spin, block, spin-rồi-block), **3 (backoff)**, 12 (`std::mutex` và họ hàng) | phần 1: backoff, so với lock thư viện |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/04 - Condition Synchronization|04 — Condition Synchronization]] | 1–3 (flag, race giữa reset và wait), **4–5 (sense-reversing, dịch sang C++, chi phí O(N))**, 9 (`std::barrier`, OpenMP), 10.1, 10.3 | **phần 2** |

**Để hiểu số đo và viết báo cáo**

| Bài | Mục | Vì sao |
|---|---|---|
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/05a - Concurrent Objects|05a — Concurrent Objects]] | 6 (linearizability), **7 (progress)** | định nghĩa "counter đúng" để thiết kế test; vì sao counter CAS chỉ lock-free còn `fetch_add` wait-free |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/00 - Motivation - Parallel and Concurrent Computing|00 — Motivation]] | speedup, Amdahl, USL; mục 6 (10 thread chậm hơn 1 thread vì mutex); mục 7 (đo cho đúng) | khung để đọc đường scalability |
| [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/09 - Performance Analysis|09 — Performance Analysis]] | $T_o$, Karp–Flatt, mục 5, bẫy B1–B13 | phương pháp đo: tải nền, P/E-core, lặp lại và trung vị |

**Đọc thêm:** [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/07 - Nonblocking Concurrent Objects|07]] mục 2.4 và 7 (CAS dưới tranh chấp cao thua mutex, backoff giúp bao nhiêu, đo trên M5); [[Note/Education/Courses/Masters-Program/Parallel-Computing/Lectures/10 - Load Balancing|10]] mục 1.2 (P-core nhanh hơn E-core 1,2–1,5 lần). Bài 06 và 08 không cần. Phần 4 (MPI) chưa có note.

**Tài liệu riêng của bài tập:** [[Note/Education/Courses/Masters-Program/Parallel-Computing/Hướng dẫn làm Assignment 1|Hướng dẫn làm Assignment 1]] (đọc trước tiên: các chỗ dễ sai, checklist), [[Note/Education/Courses/Masters-Program/Parallel-Computing/C++ cho Assignment 1 - Từ khóa, ký hiệu và memory model|C++ cho Assignment 1]] (bản rút gọn của 01–02b), [[Note/Education/Courses/Masters-Program/Parallel-Computing/Hướng dẫn C++ cho Assignment 1|Hướng dẫn C++ cho Assignment 1]], rồi `PLAN.md`, `docs/part1.md`–`part3.md` và chú thích trong `include/`.

**Nếu chỉ có 1–2 ngày:** Hướng dẫn làm Assignment 1 → C++ cho Assignment 1 → 03a mục 7, 9, 11 → 03b mục 3 → 04 mục 5 → 05a mục 7.

## Cấu trúc thư mục

| Đường dẫn | Nội dung |
|---|---|
| `include/lock.h` | các lock của phần 1 và lock thư viện dùng để so sánh |
| `include/barrier.hpp` | sense-reversing barrier và các barrier dùng để so sánh |
| `include/counter.hpp` | các counter của phần 3 |
| `include/backoff.hpp`, `platform.hpp`, `workload.hpp`, `bench_common.hpp` | chính sách chờ, phần phụ thuộc nền tảng, `is_prime`, định dạng dòng CSV |
| `tests` | bài kiểm tra tính đúng đắn của ba phần |
| `bench` | `bench_lock`, `bench_barrier`, `bench_counter`, `sleep_latency` |
| `mk` | mục tiêu `make` của từng phần (`part*.mk`) và của việc đóng gói (`zz_pack.mk`) |
| `scripts/run_bench.py` | chạy toàn bộ đợt đo, ghi số liệu thô (chỉ cần Python 3 chuẩn) |
| `scripts/plot.py` | vẽ mọi hình và sinh mọi bảng của báo cáo (cần matplotlib, numpy) |
| `scripts/machine_info.sh` | ghi cấu hình máy (macOS và Linux) |
| `scripts/fake_bench.py` | chương trình giả để thử công cụ; **không** tạo ra số đo |
| `results/<máy>/` | số liệu thô của từng máy |
| `report` | `report.tex`, `references.bib`, và `figures/` do `plot.py` sinh ra |
| `docs` | ghi chú thiết kế của từng phần (mặc định không nằm trong file zip, xem `PACK_ITEMS` trong `mk/zz_pack.mk`) |

## Yêu cầu

- Compiler C++20: Apple clang (macOS arm64), g++ hoặc clang++ (Linux x86-64/arm64); `make`.
- Python 3 (bất kỳ bản nào còn được hỗ trợ) để chạy đợt đo; không cần cài thêm gói nào.
- Chỉ cần trên máy vẽ hình và làm báo cáo: `matplotlib`, `numpy`, và XeLaTeX (TeX Live đầy đủ, có `latexmk`).
- OpenMP là tùy chọn: baseline OpenMP chỉ được biên dịch khi compiler nhận `-fopenmp` (g++ trên Linux; Apple clang thì không, trừ khi cài `libomp`). Danh sách cài đặt luôn lấy từ `build/bench_* list`, nên máy không có OpenMP vẫn chạy bình thường.

## Build và kiểm tra

```sh
make             # biên dịch các bài kiểm tra và benchmark vào build/
make test        # mọi bài kiểm tra tính đúng đắn
make tsan        # chạy lại dưới ThreadSanitizer
make mutation    # các biến thể cố ý làm hỏng phải bị bộ kiểm tra bắt
```

Chọn compiler khác: `make CXX=g++-14`.

## Chạy đợt đo trên một máy mới

Dành cho từng thành viên, trên máy của mình:

1. Giải nén mã nguồn, cắm sạc, tắt chế độ tiết kiệm điện, đóng các chương trình khác (trình duyệt, IDE, họp trực tuyến, đồng bộ đám mây).
2. `make && make test`
3. `make bench-dry` in ra lưới tham số và thời gian ước tính, không đo gì.
4. `make bench` chạy toàn bộ đợt đo (khoảng một giờ trên laptop 10 core). **Trong lúc đó đừng dùng máy.**
5. Gửi lại nguyên thư mục `results/<tên máy>/` (nén lại cũng được). Không sửa file nào trong đó.

Tên máy lấy từ hostname. Muốn đặt tên khác: `python3 scripts/run_bench.py --host ten-may`.

Thư mục `results/<máy>/` gồm:

| File | Nội dung |
|---|---|
| `lock.csv`, `barrier.csv`, `counter.csv` | mỗi lần chạy một dòng: các cột của `pc::csv_header()` (`host, arch, os, compiler, ncpu, part, impl, threads, iters, outside_work, n, seconds, ops, ops_per_s, ok, load1, extra`) cộng thêm `rep, timestamp, wall_s` |
| `calibration.json` | số vòng lặp đã chọn cho máy này (cố định trong cả đợt đo) |
| `machine.txt` | cấu hình máy do `scripts/machine_info.sh` ghi |
| `sleep_latency.csv` | thời gian thật của `sleep_for`, `yield`, lệnh chờ trong vòng spin |
| `campaign.log` | lúc bắt đầu, lúc kết thúc, load average, số lần vượt thời hạn |
| `lock_yield.csv` | chỉ có khi chạy phần tùy chọn `make bench-relax` |

Các tình huống thường gặp:

| Tình huống | Cách xử lý |
|---|---|
| `load average is ..., above 7.0` | máy chưa rảnh: đóng bớt chương trình rồi chạy lại, hoặc `python3 scripts/run_bench.py --wait 10` để chờ tối đa 10 phút. `--force` vẫn đo, nhưng số liệu đo trên máy bận thì không dùng được cho báo cáo |
| Bị ngắt giữa chừng (Ctrl-C, hết pin) | `python3 scripts/run_bench.py --resume` chạy tiếp những lần còn thiếu, giữ nguyên số vòng lặp |
| Muốn đo lại từ đầu | `python3 scripts/run_bench.py --fresh`: số liệu cũ được chuyển vào `results/<máy>/old-<thời gian>/`, không bị xóa |
| Chỉ đo một phần | `--part lock`, `--part barrier`, `--part counter` (lặp lại được); `--impl 'tas*,mcs'` để chọn cài đặt |
| Thử nhanh cả quy trình (khoảng 1–2 phút) | `make quick`: lưới rất nhỏ, 2 lần lặp, ghi vào `build/quick-results/`, không bao giờ ghi vào `results` |
| So sánh lệnh chờ ISB và YIELD (chỉ AArch64, tùy chọn) | `make bench-relax` sau khi `make bench` đã xong |

## Vẽ lại hình và biên dịch báo cáo

```sh
python3 -m venv .venv && .venv/bin/pip install matplotlib numpy     # một lần
make figures PYTHON=.venv/bin/python     # results/*/  →  report/figures/<máy>/*.pdf, tables.tex
make report                              # report/report.pdf (XeLaTeX)
make zip                                 # assignment1.zip: mã nguồn, script, results/, báo cáo
```

`scripts/plot.py` đọc **mọi** thư mục trong `results/` và sinh cho từng máy:

| File trong `report/figures/<máy>/` | Nội dung |
|---|---|
| `lock_time.pdf`, `lock_time_short.pdf` | thời gian chạy theo số thread, mỗi ô một `OUTSIDE_WORK`, kèm cận dưới lý tưởng; bản `_short` cho critical section ngắn |
| `lock_throughput.pdf` | số critical section mỗi giây |
| `lock_slowdown.pdf`, `lock_slowdown_short.pdf` | thời gian đo chia cho cận dưới lý tưởng |
| `lock_cs_length.pdf` | ảnh hưởng của `N` (độ dài critical section) |
| `lock_backoff.pdf`, `lock_backoff_short.pdf` | biến thể có backoff so với TAS/TTAS gốc |
| `lock_fairness.pdf` | thread xong sớm nhất so với thread xong muộn nhất |
| `lock_relax.pdf` | ISB so với YIELD (chỉ khi có `lock_yield.csv`) |
| `barrier_rate.pdf` | số barrier episode mỗi giây |
| `counter_time.pdf`, `counter_variants.pdf`, `counter_cas_failures.pdf` | thời gian chạy của counter, biến thể so với bản gốc, số lần CAS hỏng mỗi lần tăng |
| `tables.tex` | mọi bảng số liệu và chú thích của mọi hình, dưới dạng macro LaTeX |

Khi `results` có từ hai máy trở lên, `report/figures/cross/` có thêm hình so sánh giữa các máy; `report/figures/cross/tables.tex` luôn chứa bảng cấu hình các máy. Điểm trong hình là trung vị, thanh sai số đi từ giá trị nhỏ nhất tới lớn nhất. Thiếu file CSV hoặc thiếu một cài đặt thì hình tương ứng được bỏ qua kèm thông báo, không báo lỗi.

## Thêm một máy vào báo cáo

1. Chép thư mục `results/<tên máy>/` nhận được vào `results`.
2. `make figures PYTHON=.venv/bin/python`
3. Trong `report/report.tex` thêm tên máy vào dòng `\providecommand{\Hosts}{...}` (cách nhau bằng dấu phẩy, không có dấu cách) và thêm một dòng `\PCsetowner{<tên máy>}{<thành viên>}`.
4. `make report`

Bảng cấu hình máy, hình và bảng của máy mới, và các hình so sánh giữa các máy tự xuất hiện.

## Quy tắc đo

- **Mỗi lúc chỉ một tiến trình đo.** Không chạy hai benchmark, hay benchmark cùng với `make test`, trên cùng một máy.
- **Máy phải rảnh.** `run_bench.py` không bắt đầu khi load average 1 phút vượt 7 (đổi bằng `--load-max`), ghi load lúc bắt đầu và kết thúc vào `campaign.log`, và mỗi dòng số liệu có cột `load1`. Lưu ý: sau phút đầu, `load1` phản ánh cả chính đợt đo.
- **Lặp là vòng ngoài, các cấu hình xen kẽ.** Mỗi cấu hình chạy 7 lần; trong mỗi lượt lặp, mọi cấu hình của cả ba phần được xáo trộn với seed cố định, để tải nền hay nhiệt độ thay đổi chậm không thiên vị một cài đặt nào.
- **Số vòng lặp cố định trong cả đợt đo** (`calibration.json`). Lock dùng `I = W_N / max(threads, 1 + OUTSIDE_WORK)` để mọi lần chạy dài cỡ như nhau; barrier và counter hiệu chỉnh `I` cho từng cấu hình bằng một lần chạy thử. Vì `I` khác nhau giữa các cấu hình, hình và bảng so sánh qua thời gian quy về cùng số vòng lặp.
- **Lần chạy vượt thời hạn là số liệu, không phải lỗi**: nó được ghi thành một dòng `ok=0, extra=timeout`. Cấu hình đó được đo lại với số vòng lặp nhỏ hơn 100 lần; sau hai lần vượt thời hạn thì bỏ qua (`extra=timeout_skipped`). Lock hàng đợi (MCS) với số thread vượt số core được đo với số vòng lặp nhỏ hơn 100 lần ngay từ đầu.
- **Không lọc, không sửa số liệu thô.** `results` chỉ được ghi bởi `run_bench.py`. Muốn bỏ một lần chạy thì phải nêu lý do trong báo cáo.
- **Không chép số bằng tay.** Bảng và chú thích trong báo cáo do `plot.py` sinh ra; lời văn trích số từ các bảng đó.
- **Số liệu giả không bao giờ vào `results`.** `fake_bench.py` in `host=FAKE…`; `run_bench.py` từ chối ghi các dòng đó vào `results`, `plot.py` từ chối vẽ chúng vào `report/`, và `make zip` dừng lại nếu thấy `results/FAKE*`.

## Dành cho người sửa công cụ

- Lưới tham số nằm trong một khối ở đầu `scripts/run_bench.py` (`GRID`, `REPS`, `THREADS`, `TIMEOUT_S`, `REDUCED_ITERS`).
- Màu, ký hiệu và thứ tự của từng cài đặt nằm trong `FAMILY_STYLE` ở đầu `scripts/plot.py`; một cài đặt giữ nguyên màu và ký hiệu trong mọi hình.
- Thử cả quy trình mà không cần benchmark thật, ghi ra ngoài dự án:

  ```sh
  python3 scripts/run_bench.py --fake --quick --force --out /tmp/a1
  .venv/bin/python scripts/plot.py --results /tmp/a1 --figures /tmp/a1-figures
  ```
