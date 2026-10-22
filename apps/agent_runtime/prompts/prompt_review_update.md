[작업: 업데이트 검토] update_id={update_id}
목표: 이 업데이트가 회사 승인 제품의 기능이고 누군가의 등록 업무에 바로 쓸 수 있으면 그 사람(또는 팀)에게 보낼 활용 제안을 저장한다. 근거가 없으면 보류 사유를 남긴다.
1. get_vendor_update로 내용(제품, 플랜, 요약, 원문 링크)을 읽는다.
2. list_approved_tools를 **인자 없이** 불러 전체 목록을 받고, 업데이트의 vendor·제품이 승인 제품에 있는지와 affected_plans가 회사 플랜과 맞는지 확인한다. 'AI'·'API' 같은 category 값으로 거르면 빈 결과가 나오니 쓰지 않는다. 승인 제품이 아니거나 플랜이 다르면 save_suggestion(target_type=all, status=on_hold, hold_reason="회사 플랜에 없는 기능: …")으로 사유를 남기고 끝낸다.
3. find_relevant_tasks(update_id)로 후보 업무를 받는다. 업데이트 기능이 그 업무를 실제로 바꾸는 경우만 고른다 (예: 파일 비교·분석 기능 ↔ 리뷰·설문 분석 업무). 가격표·약관 같은 정보성 업데이트는 업무와 무관하므로 보류한다.
4. 고른 업무마다 save_suggestion(update_id, target_type=employee, target_id=employee_id, task_id, relevance=근거 한 줄, body=제안문)을 저장한다. body는 직원에게 그대로 보내는 글이다: 등록 업무 이름을 부르고, 이 기능으로 그 업무를 어떻게 하는지 2~3문장, 적용 조건(플랜) 한 줄. 제품 id·도구 이름·내부 id는 쓰지 않는다.
6. **새 모델 출시**(제목이 '새 모델 출시:'로 시작)면 3~5 대신 카드 뉴스 한 장을 만든다. find_relevant_tasks로 등록 업무와 직무(job_title)를 보고, save_suggestion(update_id, target_type=all, relevance="승인 공급사 새 모델 · 직무 N개 활용 예", body=카드)를 **한 번만** 저장한다. body는 아래 형식 그대로:
   핵심 기능
   1. (한 줄)
   2. (한 줄)
   3. (한 줄)
   이렇게 써 보세요
   • 직무명: 그 직무의 등록 업무에 이 모델을 쓰는 방법 한 문장
   핵심 기능은 업데이트 요약에 있는 것만 쓰고 지어내지 않는다. 직무는 등록 업무가 있는 것 중 3~6개, 업무가 많은 직무부터. 직무명은 job_title을 그대로 쓴다. 제품 id·내부 id·도구 이름은 쓰지 않는다.
5. (새 모델 출시가 아닐 때) 4에서 하나도 저장하지 않았을 때만 save_suggestion(target_type=all, status=on_hold, hold_reason="관련 업무 근거 없음: …")으로 사유를 남긴다. 하나라도 저장했으면 보류 기록은 만들지 않는다.
