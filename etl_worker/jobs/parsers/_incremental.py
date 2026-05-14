"""DEPRECATED — Phase 4 Step 27 (DELETE + INSERT 동기화 정책) 이후 폐기.

옛 정책 (Step 23): 처음 본 row 만 적재 (수정 무시).
- row_number_watermark(): xlsx 의 row_number 기반 (ald, sputter)
- measured_at_watermark(): rga 의 measured_at 기반

새 정책 (Step 27): DELETE + INSERT 통째 동기화.
- 같은 source_type + file_path 의 옛 row 모두 DELETE 후 모든 row 새로 INSERT
- xlsx/csv 의 현재 상태 = DB 의 현재 상태 (수정/삭제 모두 반영)
- 한 트랜잭션 안에서 DELETE + INSERT (원자성, 부분 실패 시 자동 rollback)

이 파일은 누군가 실수로 옛 함수를 import 하면 DeprecationWarning 으로 즉시 알리려고
stub 만 남겨둠. 새 코드는 이 헬퍼를 임포트하지 않음.

5 파서 변경 commit:
- Step 27 (1/6) rga_csv.py
- Step 27 (2/6) ald_rayvac.py
- Step 27 (3/6) ald_ncd.py
- Step 27 (4/6) sputter_auto.py
- Step 27 (5/6) sputter_human.py
- Step 27 (6/6) _incremental.py 폐기 (이 파일)
"""

import logging

log = logging.getLogger("etl.parsers._incremental")

log.warning(
    "_incremental.py 는 Phase 4 Step 27 이후 폐기됨. "
    "DELETE + INSERT 동기화 정책으로 대체됨. "
    "이 모듈을 import 하는 코드는 새 정책에 맞게 수정 필요."
)


def row_number_watermark(*args, **kwargs):
    """DEPRECATED. Phase 4 Step 27 이후 사용 금지."""
    raise DeprecationWarning(
        "row_number_watermark 는 Phase 4 Step 27 이후 폐기됨. "
        "DELETE + INSERT 동기화 정책으로 대체됨. "
        "파서에서 이 함수 호출 시 새 패턴 (한 트랜잭션 안에서 DELETE + INSERT) 으로 변경."
    )


def measured_at_watermark(*args, **kwargs):
    """DEPRECATED. Phase 4 Step 27 이후 사용 금지."""
    raise DeprecationWarning(
        "measured_at_watermark 는 Phase 4 Step 27 이후 폐기됨. "
        "DELETE + INSERT 동기화 정책으로 대체됨. "
        "파서에서 이 함수 호출 시 새 패턴 (한 트랜잭션 안에서 DELETE + INSERT) 으로 변경."
    )
