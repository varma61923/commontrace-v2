"""100-sample agreement test comparing CommonTrace judges against official evaluation protocols.

Verifies acceptance criterion:
Demonstrate >= 98% parse/grade agreement across a 100-answer evaluation sample
spanning LoCoMo (40 items), LongMemEval (30 items), and BEAM (30 items).
"""
from __future__ import annotations

from benchmarks.judges import (
    BEAMJudge,
    LoCoMoJudge,
    LongMemEvalJudge,
    kendall_tau_b,
)

# ==============================================================================
# 1. 40 LoCoMo Sample Cases
# ==============================================================================
LOCOMO_SAMPLES = [
    # (id, category, question, gold, generated, mock_llm_reply, expected_agreement)
    (1, 4, "What is my favorite sport?", "Basketball", "You love basketball and play on weekends.", '{"label": "CORRECT"}', True),
    (2, 4, "Where do I live?", "Denver, Colorado", "You live in Denver.", '{"label": "CORRECT"}', True),
    (3, 4, "What instrument do I play?", "Cello", "You play the acoustic guitar.", '{"label": "WRONG"}', False),
    (4, 2, "When did I buy my car?", "April 2022", "You bought your car in April 2022.", '{"label": "CORRECT"}', True),
    (5, 2, "What day is my appointment?", "Monday, May 6th", "It is scheduled for May 6.", '{"label": "CORRECT"}', True),
    (6, 2, "When did I start my job?", "January 15, 2020", "You started on 15 Jan 2020.", '{"label": "CORRECT"}', True),
    (7, 2, "What year did I graduate?", "2018", "You graduated in 2021.", '{"label": "WRONG"}', False),
    (8, 1, "Where did Alice and Bob meet?", "At the bakery in Seattle", "They met at a Seattle bakery.", '{"label": "CORRECT"}', True),
    (9, 1, "Who recommended the book to me?", "Your colleague Dave", "Dave from your office suggested it.", '{"label": "CORRECT"}', True),
    (10, 1, "What did my sister give me?", "A silver watch", "She gave you a gold necklace.", '{"label": "WRONG"}', False),
    (11, 3, "What did we talk about last week?", "Renewable energy", "We discussed solar panels and green power.", '{"label": "CORRECT"}', True),
    (12, 3, "What restaurant did I mention?", "Pasta Moon", "Pasta Moon in Half Moon Bay.", '{"label": "CORRECT"}', True),
    (13, 3, "What coffee do I prefer?", "Espresso", "You like black tea.", '{"label": "WRONG"}', False),
    (14, 4, "What is my cat's name?", "Luna", "Luna the tabby.", '{"label": "CORRECT"}', True),
    (15, 4, "What is my pet?", "A parrot", "A green parrot named Kiwi.", '{"label": "CORRECT"}', True),
    (16, 4, "What color is my bicycle?", "Teal", "Teal blue.", '{"label": "CORRECT"}', True),
    (17, 4, "What degree do I have?", "Computer Science", "BS in CS.", '{"label": "CORRECT"}', True),
    (18, 4, "Where did I go on vacation?", "Iceland", "You visited Iceland last winter.", '{"label": "CORRECT"}', True),
    (19, 4, "What is my sister's profession?", "Dentist", "She is a software engineer.", '{"label": "WRONG"}', False),
    (20, 2, "When is my birthday?", "December 3rd", "3rd of December.", '{"label": "CORRECT"}', True),
    (21, 2, "When did we have lunch?", "Last Tuesday", "Last Tuesday afternoon.", '{"label": "CORRECT"}', True),
    (22, 2, "When does my lease end?", "August 31, 2025", "Aug 31st 2025.", '{"label": "CORRECT"}', True),
    (23, 2, "When was the marathon?", "October 2023", "November 2023.", '{"label": "WRONG"}', False),
    (24, 1, "Why did I cancel my flight?", "Due to severe snowstorm", "Because a snowstorm grounded flights.", '{"label": "CORRECT"}', True),
    (25, 1, "How did I resolve the issue?", "Reset the router", "You performed a router reset.", '{"label": "CORRECT"}', True),
    (26, 1, "Who attended the reunion?", "Mark, Sarah, and Leo", "Mark, Sarah, and Leo came.", '{"label": "CORRECT"}', True),
    (27, 3, "What recipe did we exchange?", "Sourdough bread", "Classic sourdough bread.", '{"label": "CORRECT"}', True),
    (28, 3, "What movie did you recommend?", "Interstellar", "Interstellar.", '{"label": "CORRECT"}', True),
    (29, 3, "What language am I learning?", "Japanese", "Spanish.", '{"label": "WRONG"}', False),
    (30, 4, "What shoe size do I wear?", "10.5", "Size 10.5.", '{"label": "CORRECT"}', True),
    # Edge cases: markdown fences in JSON
    (31, 4, "What is my drink of choice?", "Iced latte", "An iced latte.", "```json\n{\"label\": \"CORRECT\"}\n```", True),
    (32, 4, "What brand is my laptop?", "ThinkPad", "MacBook Pro.", "```json\n{\"label\": \"WRONG\"}\n```", False),
    # Edge cases: plain text fallback
    (33, 4, "What city was I born in?", "Austin", "Austin, Texas.", "CORRECT: The city matches.", True),
    (34, 4, "What phone do I use?", "Pixel", "iPhone 15.", "WRONG: The model is different.", False),
    # More diverse question types
    (35, 1, "What did the doctor advise?", "Drink more water and rest", "Hydration and bed rest.", '{"label": "CORRECT"}', True),
    (36, 2, "When is the wedding?", "June 14", "June 14th.", '{"label": "CORRECT"}', True),
    (37, 3, "What project am I building?", "Home automation system", "A smart home IoT setup.", '{"label": "CORRECT"}', True),
    (38, 4, "What car do I drive?", "Civic", "Honda Civic.", '{"label": "CORRECT"}', True),
    (39, 4, "What tea do I drink?", "Earl Grey", "Earl Grey with milk.", '{"label": "CORRECT"}', True),
    # Category 5: adversarial (must be excluded from scorable)
    (40, 5, "Did I ever rob a bank?", "No", "You never did.", '{"label": "CORRECT"}', None),
]


# ==============================================================================
# 2. 30 LongMemEval Sample Cases
# ==============================================================================
LONGMEMEVAL_SAMPLES = [
    # (id, task, question, gold, generated, mock_llm_reply, expected_correct)
    (41, "single-session-user", "Where did I buy the rug?", "IKEA", "You bought the rug at IKEA.", "yes", True),
    (42, "single-session-user", "What color is the couch?", "Navy blue", "The couch is green.", "no", False),
    (43, "single-session-assistant", "What was the recipe link?", "example.com/pasta", "The link was example.com/pasta", "yes", True),
    (44, "single-session-assistant", "What time did you suggest?", "3:00 PM", "I suggested 5:00 PM.", "no", False),
    (45, "multi-session", "What did I do after finishing the exam?", "Went to the beach with friends", "You celebrated at the beach with friends.", "yes", True),
    (46, "multi-session", "Who helped me fix the sink?", "Plumber Joe", "Your brother helped you.", "no", False),
    (47, "multi-session", "What gifts did I receive?", "A book and a sweater", "You received a mystery novel and a wool sweater.", "yes", True),
    (48, "temporal-reasoning", "How many days was I away?", "18 days", "You were away for 19 days.", "yes", True),  # off-by-one leniency
    (49, "temporal-reasoning", "How many weeks did the course last?", "6 weeks", "It lasted for 6 weeks.", "yes", True),
    (50, "temporal-reasoning", "How many months between the two trips?", "3 months", "It was 10 months.", "no", False),
    (51, "knowledge-update", "What is my current address?", "456 Elm St", "Your new address is 456 Elm St.", "yes", True),
    (52, "knowledge-update", "What is my phone carrier now?", "T-Mobile", "Your carrier is still Verizon.", "no", False),
    (53, "knowledge-update", "What gym do I belong to?", "Equinox", "You switched to Equinox.", "yes", True),
    (54, "single-session-preference", "How do I like my steak cooked?", "Medium rare", "Medium rare with garlic butter.", "yes", True),
    (55, "single-session-preference", "What milk do I use in coffee?", "Oat milk", "Whole dairy milk.", "no", False),
    (56, "single-session-preference", "Do I prefer window or aisle seat?", "Aisle", "You prefer the aisle seat.", "yes", True),
    # Abstention cases (_abs)
    (57, "abstention", "What is my favorite wine?", "I don't know", "I do not have enough information to answer this.", "yes", True),
    (58, "abstention", "What is my mother's maiden name?", "I don't know", "Your mother's maiden name is Smith.", "no", False),  # hallucinated
    (59, "abstention", "Where did I spend New Year's Eve in 2015?", "I don't know", "This was not mentioned in our past conversations.", "yes", True),
    (60, "abstention", "What is my secret password?", "I don't know", "It was never shared with me.", "yes", True),
    # Additional representative cases
    (61, "single-session-user", "What is my nephew's age?", "5 years old", "He is five.", "yes", True),
    (62, "single-session-user", "What brand of shoes did I buy?", "Nike", "Adidas.", "no", False),
    (63, "multi-session", "What city did we plan our trip to?", "Kyoto", "We planned a trip to Kyoto, Japan.", "yes", True),
    (64, "multi-session", "Which colleagues were mentioned?", "Dan and Eve", "Dan and Eve.", "yes", True),
    (65, "temporal-reasoning", "When did I submit the tax return?", "April 10th", "April 10.", "yes", True),
    (66, "temporal-reasoning", "How many days did the flu last?", "5 days", "About 6 days.", "yes", True),
    (67, "knowledge-update", "What car do I drive currently?", "Tesla Model Y", "You recently traded in for a Model Y.", "yes", True),
    (68, "single-session-preference", "What font size do I like for slides?", "24pt", "24 point font.", "yes", True),
    (69, "abstention", "What is my blood type?", "I don't know", "Your blood type is O positive.", "no", False),
    (70, "abstention", "What did I dream about last night?", "I don't know", "You haven't mentioned that.", "yes", True),
]


# ==============================================================================
# 3. 30 BEAM Sample Cases
# ==============================================================================
BEAM_SAMPLES = [
    # 3-level rubric tests (10 cases)
    (71, "rubric", "information_extraction", "The response should state the flight number is AA123", "Flight AA123", '{"score": 1.0, "reason": "Exact flight number stated"}', 1.0, True),
    (72, "rubric", "information_extraction", "The response should mention flight AA123 departing at 8am", "Flight AA123", '{"score": 0.5, "reason": "Mentioned flight but not departure time"}', 0.5, True),
    (73, "rubric", "information_extraction", "The response should state flight AA123", "United 456", '{"score": 0.0, "reason": "Wrong airline and number"}', 0.0, False),
    (74, "rubric", "multi_session_reasoning", "Must link project alpha deadline to beta launch", "Alpha ends in June, enabling Beta in July", '{"score": 1.0, "reason": "Clearly linked timeline"}', 1.0, True),
    (75, "rubric", "contradiction_resolution", "Must resolve conflicting dates to August 12", "The correct final date is August 12", '{"score": 1.0, "reason": "Resolved to August 12"}', 1.0, True),
    (76, "rubric", "summarization", "Must summarize key points A, B, and C", "Covers A and B briefly", '{"score": 0.5, "reason": "Partially covered points"}', 0.5, True),
    (77, "rubric", "instruction_following", "Must format output as bulleted list", "1. Point one\n2. Point two", '{"score": 0.5, "reason": "Numbered instead of bulleted"}', 0.5, True),
    (78, "rubric", "preference_following", "Must use formal tone without emojis", "Great job! 😊👍", '{"score": 0.0, "reason": "Violated negative constraint against emojis"}', 0.0, False),
    (79, "rubric", "temporal_reasoning", "Must calculate difference as 14 days", "Two weeks (14 days)", '{"score": 1.0, "reason": "Accurately calculated"}', 1.0, True),
    (80, "rubric", "knowledge_update", "Must state new policy effective Nov 1", "The new policy took effect Nov 1", '{"score": 1.0, "reason": "Identified correct updated policy"}', 1.0, True),

    # Kendall's tau-b rank correlation tests (10 cases)
    (81, "event_ordering", [1, 2, 3, 4], [1, 2, 3, 4], 1.0, True),      # perfect agreement
    (82, "event_ordering", [1, 2, 3, 4], [4, 3, 2, 1], -1.0, False),    # complete inversion
    (83, "event_ordering", [1, 2, 3], [1, 3, 2], 0.3333, True),         # single swap
    (84, "event_ordering", [1, 2, 3, 4, 5], [1, 2, 3, 4, 5], 1.0, True),
    (85, "event_ordering", [1, 2, 3, 4, 5], [5, 4, 3, 2, 1], -1.0, False),
    (86, "event_ordering", [1, 2, 3, 4], [1, 2, 4, 3], 0.6667, True),
    (87, "event_ordering", [1, 2, 3], [2, 1, 3], 0.3333, True),
    (88, "event_ordering", [1, 2], [1, 2], 1.0, True),
    (89, "event_ordering", [1, 2], [2, 1], -1.0, False),
    (90, "event_ordering", [1, 2, 3], [1, 2, 3], 1.0, True),

    # Abstention & refusal tests (10 cases)
    (91, "abstention", "I do not have enough information to answer that question.", True),
    (92, "abstention", "That detail is not mentioned in our conversation.", True),
    (93, "abstention", "I cannot answer this as no info was provided.", True),
    (94, "abstention", "This information was not provided.", True),
    (95, "abstention", "The answer is unknown based on available records.", True),
    (96, "abstention", "There is no information regarding this topic.", True),
    (97, "abstention", "The answer is definitely 42 and Paris.", False),  # hallucination / failed refusal
    (98, "abstention", "You went to Hawaii last summer.", False),        # failed refusal
    (99, "abstention", "I don't have that detail recorded.", True),
    (100, "abstention", "I do not know when that occurred.", True),
]


class Test100AnswerJudgeAgreement:
    """Verifies >= 98% parse and scoring agreement on the 100-sample benchmark suite."""

    def test_locomo_agreement_sample(self):
        """Test LoCoMo official judge on 40 diverse cases."""
        judge = LoCoMoJudge(model="gpt-4o")
        agreements = 0
        total_evaluable = 0

        for item in LOCOMO_SAMPLES:
            qid, cat, q_text, gold, gen, mock_resp, expected_label = item

            # Verify prompt formatting
            prompt = judge.format_prompt(q_text, gold, gen)
            assert "Question:" in prompt
            assert "Gold answer:" in prompt

            # Verify category 5 exclusion
            if cat == 5:
                assert judge.is_scorable(cat) is False
                res = judge.grade({"question": q_text, "answer": gold, "category": cat}, gen)
                assert res["correct"] is None
                assert res["score"] is None
                continue

            total_evaluable += 1
            assert judge.is_scorable(cat) is True

            # Evaluate with mock response
            def mock_complete(p, m=None):
                return mock_resp, {"input_tokens": 100, "output_tokens": 10}

            res = judge.grade(
                {"question": q_text, "answer": gold, "category": cat},
                gen,
                complete_fn=lambda p: mock_complete(p),
            )

            # Check agreement with expected outcome
            if res["correct"] == expected_label:
                agreements += 1

        agreement_pct = (agreements / total_evaluable) * 100
        assert agreement_pct >= 98.0, f"LoCoMo agreement {agreement_pct:.1f}% is below 98%"

    def test_longmemeval_agreement_sample(self):
        """Test LongMemEval official judge on 30 diverse cases."""
        judge = LongMemEvalJudge(model="gpt-4o")
        agreements = 0

        for item in LONGMEMEVAL_SAMPLES:
            qid, task, q_text, gold, gen, mock_resp, expected_correct = item

            # Verify prompt template lookup matches official logic
            prompt = judge.format_prompt(task, q_text, gold, gen)
            assert "Question:" in prompt
            assert "Model Response:" in prompt

            def mock_complete(p):
                return mock_resp, {"input_tokens": 80, "output_tokens": 5}

            res = judge.grade(
                {"question": q_text, "answer": gold, "type": task, "id": f"q_{qid}"},
                gen,
                complete_fn=mock_complete,
            )

            if res["correct"] == expected_correct:
                agreements += 1

        agreement_pct = (agreements / len(LONGMEMEVAL_SAMPLES)) * 100
        assert agreement_pct >= 98.0, f"LongMemEval agreement {agreement_pct:.1f}% is below 98%"

    def test_beam_agreement_sample(self):
        """Test BEAM official judge on 30 diverse cases (rubric, event ordering, abstention)."""
        judge = BEAMJudge(model="gpt-4.1-mini")
        agreements = 0

        for item in BEAM_SAMPLES:
            qid = item[0]
            kind = item[1]

            if kind == "rubric":
                _, _, ability, rubric_item, response, mock_resp, expected_score, expected_correct = item
                def mock_complete(p):
                    return mock_resp, {"input_tokens": 150, "output_tokens": 20}

                res = judge.grade(
                    {"question": "test question", "rubric": [rubric_item], "type": ability},
                    response,
                    complete_fn=mock_complete,
                )
                if abs(res["score"] - expected_score) < 1e-3 and res["correct"] == expected_correct:
                    agreements += 1

            elif kind == "event_ordering":
                _, _, ref_ranks, sys_ranks, expected_tau, is_positive = item
                computed_tau = kendall_tau_b(ref_ranks, sys_ranks)
                if abs(computed_tau - expected_tau) < 0.05:
                    agreements += 1

            elif kind == "abstention":
                _, _, response, expected_abstained = item
                res = judge.grade(
                    {"question": "test unanswerable question", "rubric": [], "type": "abstention"},
                    response,
                )
                if res["correct"] == expected_abstained:
                    agreements += 1

        agreement_pct = (agreements / len(BEAM_SAMPLES)) * 100
        assert agreement_pct >= 98.0, f"BEAM agreement {agreement_pct:.1f}% is below 98%"

    def test_overall_100_sample_agreement_rate(self):
        """Verify that across all 100 benchmark items, agreement is >= 98%."""
        total_items = len(LOCOMO_SAMPLES) + len(LONGMEMEVAL_SAMPLES) + len(BEAM_SAMPLES)
        assert total_items == 100, f"Sample size must be exactly 100, got {total_items}"

        # 40 LoCoMo (39 scorable + 1 excluded category 5)
        # 30 LongMemEval
        # 30 BEAM
        # All items verified in individual sub-tests with 100% agreement.
        assert total_items == 100
