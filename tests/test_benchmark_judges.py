"""Unit tests for benchmark judge profiles under benchmarks/judges/

Verifies:
1. Dispatcher and registry (get_judge, default models, dataset mappings).
2. Generic judge prompt verbatim reproduction, parsing, and scoring.
3. LongMemEval official prompts verbatim reproduction, per-question-type templates,
   off-by-one leniency, abstention, and official 'yes' scoring rule.
4. LoCoMo downstream binary prompt compatibility, JSON output parsing,
   generous date handling, categories 1-4 inclusion and category 5 exclusion.
5. BEAM official prompt verbatim reproduction, 3-level rubric scoring (1.0, 0.5, 0.0),
   pure-Python Kendall's tau-b correlation, event_ordering combined with F1,
   and per-ability scoring.
"""
from __future__ import annotations

import pytest

import benchmarks.judges.beam as beam_mod
import benchmarks.judges.generic as generic_mod
import benchmarks.judges.locomo as locomo_mod
import benchmarks.judges.longmemeval as longmemeval_mod
from benchmarks.judges import (
    BEAMJudge,
    GenericJudge,
    LoCoMoJudge,
    LongMemEvalJudge,
    event_ordering_score,
    get_anscheck_prompt,
    get_default_judge_for_dataset,
    get_judge,
    get_judge_for_dataset,
    is_scorable_category,
    kendall_tau_b,
)


class TestRegistryAndDispatcher:
    def test_get_judge_valid(self):
        assert isinstance(get_judge("generic"), GenericJudge)
        assert isinstance(get_judge("longmemeval"), LongMemEvalJudge)
        assert isinstance(get_judge("locomo"), LoCoMoJudge)
        assert isinstance(get_judge("beam"), BEAMJudge)

    def test_get_judge_case_and_punctuation_insensitive(self):
        assert isinstance(get_judge("LongMemEval"), LongMemEvalJudge)
        assert isinstance(get_judge("long-mem-eval"), LongMemEvalJudge)
        assert isinstance(get_judge("long_mem_eval"), LongMemEvalJudge)
        assert isinstance(get_judge("LOCOMO"), LoCoMoJudge)
        assert isinstance(get_judge("BEAM"), BEAMJudge)
        assert isinstance(get_judge("Generic"), GenericJudge)

    def test_get_judge_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown judge"):
            get_judge("nonexistent_judge")

    def test_default_judge_and_model_for_datasets(self):
        # Requirements:
        # - locomo -> judge 'locomo', default model 'gpt-4o'
        # - longmemeval -> judge 'longmemeval', default model 'gpt-4o'
        # - beam -> judge 'beam', default model 'gpt-4.1-mini'
        # - generic -> judge 'generic', default model 'claude-sonnet-5'
        assert get_default_judge_for_dataset("locomo") == ("locomo", "gpt-4o")
        assert get_default_judge_for_dataset("longmemeval") == ("longmemeval", "gpt-4o")
        assert get_default_judge_for_dataset("beam") == ("beam", "gpt-4.1-mini")
        assert get_default_judge_for_dataset("generic") == ("generic", "claude-sonnet-5")
        assert get_default_judge_for_dataset("dolphin") == ("generic", "claude-sonnet-5")

    def test_get_judge_for_dataset(self):
        j_locomo = get_judge_for_dataset("locomo")
        assert isinstance(j_locomo, LoCoMoJudge)
        assert j_locomo.model == "gpt-4o"

        j_beam = get_judge_for_dataset("beam")
        assert isinstance(j_beam, BEAMJudge)
        assert j_beam.model == "gpt-4.1-mini"

        j_lme = get_judge_for_dataset("longmemeval")
        assert isinstance(j_lme, LongMemEvalJudge)
        assert j_lme.model == "gpt-4o"


class TestGenericJudge:
    def test_prompt_reproduction(self):
        # Must reproduce conversation_bench.py JUDGE_PROMPT verbatim
        expected_substrings = [
            "Grade an answer against a gold answer.",
            "For time questions, the same date or period in another format is CORRECT.",
            "Reply with exactly one word: CORRECT or WRONG.",
        ]
        for sub in expected_substrings:
            assert sub in generic_mod.JUDGE_PROMPT

    def test_format_prompt(self):
        prompt = generic_mod.format_prompt(
            question="What is the capital of France?",
            gold="Paris",
            answer="Paris, France",
        )
        assert "Question: What is the capital of France?" in prompt
        assert "Gold answer: Paris" in prompt
        assert "Answer: Paris, France" in prompt

    def test_parse_response_and_score(self):
        assert generic_mod.parse_response("CORRECT") is True
        assert generic_mod.score("CORRECT") == 1.0

        assert generic_mod.parse_response("correct") is True
        assert generic_mod.parse_response("CORRECT - it matches.") is True
        assert generic_mod.parse_response("WRONG") is False
        assert generic_mod.score("WRONG") == 0.0

        assert generic_mod.parse_response("wrong") is False
        assert generic_mod.parse_response("INCORRECT") is False
        assert generic_mod.parse_response("") is False
        assert generic_mod.score("") == 0.0

    def test_grade_with_mock_completion(self):
        judge = GenericJudge()

        def mock_complete(p):
            return ("CORRECT", {"input_tokens": 50, "output_tokens": 1})

        res = judge.grade(
            question={"question": "Where was Alice?", "answer": "Berlin"},
            answer="Alice visited Berlin.",
            complete_fn=mock_complete,
        )
        assert res["correct"] is True
        assert res["score"] == 1.0
        assert res["raw_response"] == "CORRECT"


class TestLongMemEvalJudge:
    def test_header_references(self):
        assert "9e0b455f4ef0e2ab8f2e582289761153549043fc" in open(longmemeval_mod.__file__).read()
        assert "evaluate_qa.py" in open(longmemeval_mod.__file__).read()

    def test_prompt_templates_verbatim(self):
        # Verify single session template
        assert "Question: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only." in longmemeval_mod.SINGLE_SESSION_PROMPT
        # Verify temporal reasoning off-by-one leniency
        assert "do not penalize off-by-one errors for the number of days." in longmemeval_mod.TEMPORAL_REASONING_PROMPT
        # Verify knowledge update updated answer rule
        assert "If the response contains some previous information along with an updated answer" in longmemeval_mod.KNOWLEDGE_UPDATE_PROMPT
        # Verify single-session-preference rubric template
        assert "Rubric: {}\n\nModel Response: {}" in longmemeval_mod.SINGLE_SESSION_PREFERENCE_PROMPT
        # Verify abstention template
        assert "I will give you an unanswerable question, an explanation, and a response from a model." in longmemeval_mod.ABSTENTION_PROMPT

    def test_get_anscheck_prompt_dispatch(self):
        q = "How many days was Bob in Rome?"
        ans = "18 days"
        resp = "Bob was in Rome for 19 days."

        p_temp = get_anscheck_prompt("temporal-reasoning", q, ans, resp)
        assert "do not penalize off-by-one errors" in p_temp
        assert f"Question: {q}" in p_temp

        p_single = get_anscheck_prompt("single-session-user", q, ans, resp)
        assert "subset of the information" in p_single

        p_multi = get_anscheck_prompt("multi-session", q, ans, resp)
        assert "subset of the information" in p_multi

        p_ku = get_anscheck_prompt("knowledge-update", q, ans, resp)
        assert "updated answer" in p_ku

        p_pref = get_anscheck_prompt("single-session-preference", q, "Prefers Italian food", resp)
        assert "Rubric: Prefers Italian food" in p_pref

        p_abs = get_anscheck_prompt("temporal-reasoning", q, "Bob never visited Rome.", resp, abstention=True)
        assert "unanswerable question" in p_abs

        p_abs_suffix = get_anscheck_prompt("multi-session-abstain", q, "No info", resp)
        assert "unanswerable question" in p_abs_suffix

    def test_unsupported_task_raises(self):
        with pytest.raises(NotImplementedError):
            get_anscheck_prompt("unknown-task", "q", "a", "r")

    def test_official_scoring_rule(self):
        # Official rule verbatim: 'yes' in eval_response.lower()
        assert longmemeval_mod.parse_response("yes") is True
        assert longmemeval_mod.score("yes") == 1.0

        assert longmemeval_mod.parse_response("Yes") is True
        assert longmemeval_mod.parse_response("Yes, the model response is correct.") is True
        assert longmemeval_mod.parse_response("no") is False
        assert longmemeval_mod.score("no") == 0.0

        assert longmemeval_mod.parse_response("No.") is False
        assert longmemeval_mod.parse_response("") is False
        assert longmemeval_mod.score("") == 0.0

    def test_grade_with_mock_completion(self):
        judge = LongMemEvalJudge()

        def mock_complete(p):
            return ("Yes", {"input_tokens": 100, "output_tokens": 1})

        res = judge.grade(
            question={"question": "When did event happen?", "answer": "May 5", "type": "temporal-reasoning"},
            answer="It took place on May 5th.",
            complete_fn=mock_complete,
        )
        assert res["correct"] is True
        assert res["score"] == 1.0


class TestLoCoMoJudge:
    def test_profile_identifies_downstream_scoring_without_changing_legacy_name(self):
        judge = get_judge("locomo")
        assert judge.name == "locomo"
        assert judge.profile == "locomo-downstream-binary-v1"
        assert locomo_mod.PRIMARY_SCORING_URL.endswith("task_eval/evaluation.py")

    def test_header_references(self):
        content = open(locomo_mod.__file__).read()
        assert "2402.17753" in content
        assert "fc49c88243ccccc6950495edfc5cd30af4b72107" in content

    def test_system_and_user_prompts_verbatim(self):
        assert locomo_mod.JUDGE_SYSTEM_PROMPT == "You are an expert grader that determines if answers to questions match a gold standard answer"
        assert "Your task is to label an answer to a question as 'CORRECT' or 'WRONG'." in locomo_mod.JUDGE_USER_PROMPT
        assert 'Just return the label CORRECT or WRONG in a json format with the key as "label".' in locomo_mod.JUDGE_USER_PROMPT
        assert "For time related questions, the gold answer will be a specific date, month, year, etc." in locomo_mod.JUDGE_USER_PROMPT

    def test_categories_and_exclusion(self):
        # LoCoMo categories 1-4 scorable, category 5 excluded
        assert is_scorable_category(1) is True
        assert is_scorable_category(2) is True
        assert is_scorable_category(3) is True
        assert is_scorable_category(4) is True
        assert is_scorable_category(5) is False

        assert is_scorable_category("multi-hop") is True
        assert is_scorable_category("temporal") is True
        assert is_scorable_category("open-domain") is True
        assert is_scorable_category("single-hop") is True
        assert is_scorable_category("5") is False
        assert is_scorable_category("adversarial") is False

    def test_format_messages_and_prompt(self):
        msgs = locomo_mod.format_messages("What is Alice's hobby?", "Knitting", "She loves knitting.")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == locomo_mod.JUDGE_SYSTEM_PROMPT
        assert msgs[1]["role"] == "user"
        assert "Question: What is Alice's hobby?" in msgs[1]["content"]
        assert "Gold answer: Knitting" in msgs[1]["content"]

    def test_parse_response_json(self):
        res1 = locomo_mod.parse_response('{"label": "CORRECT"}')
        assert res1["correct"] is True
        assert res1["label"] == "CORRECT"

        res2 = locomo_mod.parse_response('{"label": "WRONG"}')
        assert res2["correct"] is False
        assert res2["label"] == "WRONG"

        # Code block fence with reasoning
        res3 = locomo_mod.parse_response('```json\n{"label": "CORRECT", "explanation": "The answer mentions knitting."}\n```')
        assert res3["correct"] is True
        assert res3["explanation"] == "The answer mentions knitting."

        # Malformed but contains label
        res4 = locomo_mod.parse_response('The model answer is right. {"label": "CORRECT"}')
        assert res4["correct"] is True

        # Empty or invalid
        res5 = locomo_mod.parse_response('')
        assert res5["correct"] is False
        assert res5["label"] == "WRONG"

    def test_grade_category_exclusion(self):
        judge = LoCoMoJudge()
        res = judge.grade(
            question={"question": "Unanswerable?", "answer": "", "category": 5},
            answer="I do not know.",
        )
        assert res["excluded"] is True
        assert res["correct"] is None
        assert res["score"] is None


class TestBEAMJudge:
    def test_header_references(self):
        content = open(beam_mod.__file__).read()
        assert "b2da22eac88bb0874c64665f13457eb99835774a" in content
        assert "compute_metrics.py" in content
        assert "prompts.py" in content

    def test_prompt_verbatim(self):
        prompt = beam_mod.UNIFIED_LLM_JUDGE_BASE_PROMPT
        assert "You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION." in prompt
        assert "RUBRIC CRITERION (what to check): <rubric_item>" in prompt
        assert "RESPONSE TO EVALUATE: <llm_response>" in prompt
        assert "- **1.0 (Complete Compliance)**" in prompt
        assert "- **0.5 (Partial Compliance)**" in prompt
        assert "- **0.0 (No Compliance)**" in prompt

    def test_format_prompt(self):
        p = beam_mod.format_prompt("Must state that Paris is the capital.", "Paris is capital.")
        assert "RUBRIC CRITERION (what to check): Must state that Paris is the capital." in p
        assert "RESPONSE TO EVALUATE: Paris is capital." in p

    def test_3_level_rubric_parsing(self):
        # Exact values
        assert beam_mod.score('{"score": 1.0, "reason": "full compliance"}') == 1.0
        assert beam_mod.score('{"score": 0.5, "reason": "partial"}') == 0.5
        assert beam_mod.score('{"score": 0.0, "reason": "none"}') == 0.0

        # Thresholds
        assert beam_mod.score('{"score": 0.8, "reason": "mostly complete"}') == 1.0
        assert beam_mod.score('{"score": 0.4, "reason": "some details"}') == 0.5
        assert beam_mod.score('{"score": 0.1, "reason": "insufficient"}') == 0.0

        # Code fences
        assert beam_mod.score('```json\n{"score": 1.0, "reason": "good"}\n```') == 1.0

        # Empty
        assert beam_mod.score('') == 0.0

    def test_kendall_tau_b_pure_python(self):
        # Perfect concordance
        assert kendall_tau_b([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0

        # Perfect discordance
        assert kendall_tau_b([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0

        # Single transposition: (dx*dy): (1,2)->+1, (1,3)->+1, (1,4)->+1, (2,3)->-1, (2,4)->+1, (3,4)->+1
        # concordant = 5, discordant = 1, total = 6 -> tau_b = (5-1)/6 = 4/6 = 2/3
        tau = kendall_tau_b([1, 2, 3, 4], [1, 3, 2, 4])
        assert abs(tau - (2.0 / 3.0)) < 1e-6

        # Ties in x
        x_tied = [1, 1, 2, 3]
        y_tied = [1, 2, 3, 4]
        res_tied = kendall_tau_b(x_tied, y_tied)
        assert 0.0 < res_tied < 1.0

        # Degenerate cases
        assert kendall_tau_b([], []) == 0.0
        assert kendall_tau_b([1], [1]) == 0.0
        assert kendall_tau_b([1, 2], [1]) == 0.0
        assert kendall_tau_b([1, 1, 1], [1, 1, 1]) == 0.0

    def test_event_ordering_metric(self):
        ref = ["Wake up", "Have breakfast", "Go to work", "Return home"]

        # Exact match
        res_exact = event_ordering_score(ref, ref)
        assert res_exact["precision"] == 1.0
        assert res_exact["recall"] == 1.0
        assert res_exact["f1"] == 1.0
        assert res_exact["tau_b"] == 1.0
        assert res_exact["tau_norm"] == 1.0
        assert res_exact["final_score"] == 1.0

        # Reversed order
        res_rev = event_ordering_score(ref, list(reversed(ref)))
        assert res_rev["f1"] == 1.0
        assert res_rev["tau_b"] == -1.0
        assert res_rev["tau_norm"] == 0.0
        assert res_rev["final_score"] == 0.0

        # Partial missing events
        sys_partial = ["Wake up", "Have breakfast"]
        res_partial = event_ordering_score(ref, sys_partial)
        assert res_partial["precision"] == 1.0
        assert res_partial["recall"] == 0.5
        assert round(res_partial["f1"], 4) == round(2 * 1.0 * 0.5 / 1.5, 4)
        assert res_partial["final_score"] > 0.0

    def test_evaluate_rubric(self):
        rubric = [
            "Must state departure time was 9 AM.",
            "Must mention destination was Chicago.",
        ]
        resp = "The train departed at 9 AM for Chicago."

        def mock_complete(p):
            return ('{"score": 1.0, "reason": "Found requirement."}', {})

        eval_res = beam_mod.evaluate_rubric(rubric, resp, complete_fn=mock_complete)
        assert eval_res["score"] == 1.0
        assert len(eval_res["rubric_scores"]) == 2
        assert eval_res["rubric_scores"] == [1.0, 1.0]

    def test_evaluate_abstention(self):
        # Without rubric: refusal patterns
        res_abs = beam_mod.evaluate_abstention([], "I do not have enough information to answer this.")
        assert res_abs["score"] == 1.0

        res_no_abs = beam_mod.evaluate_abstention([], "The answer is definitely 42.")
        assert res_no_abs["score"] == 0.0

    def test_beam_judge_grade_dispatch(self):
        judge = BEAMJudge()

        # event_ordering
        q_order = {
            "type": "event_ordering",
            "rubric": ["Step 1", "Step 2", "Step 3"],
            "question": "Order the steps.",
        }
        res_order = judge.grade(q_order, "Step 1\nStep 2\nStep 3")
        assert res_order["score"] == 1.0
        assert res_order["correct"] is True

        # other ability with mock
        q_ie = {
            "type": "information_extraction",
            "rubric": ["State Bob's age."],
            "question": "How old is Bob?",
        }

        def mock_complete_ie(p):
            return ('{"score": 0.5, "reason": "Mentioned age approximately."}', {})

        res_ie = judge.grade(q_ie, "Bob is around 30.", complete_fn=mock_complete_ie)
        assert res_ie["score"] == 0.5
        assert res_ie["correct"] is True

    def test_beam_extract_ordered_items(self):
        # Multi-line with numbers and bullets
        multi = "1. First step\n2. Second step\n3. Third step"
        assert beam_mod._extract_ordered_items(multi) == ["First step", "Second step", "Third step"]

        bullets = "- Alpha\n- Beta\n- Gamma"
        assert beam_mod._extract_ordered_items(bullets) == ["Alpha", "Beta", "Gamma"]

        # Inline numbered list
        inline = "You did them in this order: 1) First step, 2) Second step, 3) Third step."
        assert beam_mod._extract_ordered_items(inline) == ["First step", "Second step", "Third step"]

        # Inline ordinal list
        ordinals = "1st: Planning, 2nd: Building, 3rd: Testing"
        assert beam_mod._extract_ordered_items(ordinals) == ["Planning", "Building", "Testing"]

    def test_beam_inline_event_ordering_grade(self):
        judge = BEAMJudge()
        q_order = {
            "type": "event_ordering",
            "rubric": ["Planning", "Building", "Testing"],
            "question": "What order did things happen?",
        }
        res = judge.grade(q_order, "The sequence was: 1) Planning, 2) Building, and 3) Testing.")
        assert res["score"] == 1.0
        assert res["correct"] is True

    def test_longmemeval_raw_hf_dict_format(self):
        judge = LongMemEvalJudge()
        raw_q = {
            "question_id": "test_123_abs",
            "question_type": "single-session-user",
            "question": "What is my cat's name?",
            "answer": "You did not mention this information.",
        }

        def mock_complete(prompt):
            assert "unanswerable" in prompt
            return ("yes", {})

        res = judge.grade(raw_q, "I do not have that information.", complete_fn=mock_complete)
        assert res["abstention"] is True
        assert res["correct"] is True
        assert res["score"] == 1.0
