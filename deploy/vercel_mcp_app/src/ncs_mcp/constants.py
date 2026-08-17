from __future__ import annotations


# 추천 점수 가중치
SCORE_WEIGHTS = {
    "coverage": 0.40,
    "match_basis": 0.25,
    "level_fit": 0.20,
    "density": 0.10,
    "specificity": 0.05,
}

# 매칭 근거 강도
MATCH_BASIS_WEIGHTS = {
    "training_goal_concept_text": 1.0,
    "training_goal_concept_token": 0.7,
    "training_goal_element_implied": 0.4,
    "training_goal_element_implied_concept": 0.4,
    "training_goal_unit_core_concept": 0.2,
    "unit_ksa_concept_inherited": 0.2,
}

# 범용 KSA 페널티 임계값
GENERIC_KSA_UNIT_THRESHOLD = 10

# 추천 결과 한도
MAX_RECOMMENDATIONS = 10
DEFAULT_RECOMMENDATIONS = 5

# Top-K 다양성 제한
MAX_SAME_SUB_CODE_IN_TOP_K = 2
