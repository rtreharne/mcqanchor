from openai import OpenAI


class ChatbotError(Exception):
    pass


SYSTEM_INSTRUCTION = """
You are the MCQ Anchor product assistant. Answer questions clearly and concisely using only the supplied MCQ Anchor product information. MCQ Anchor is currently a pilot-stage concept and product website. Do not claim that features have already been deployed, tested or adopted unless the supplied information explicitly says so. Do not invent pricing, customer names, integrations, timelines or evidence. When a visitor asks about something that has not yet been decided, say that it is configurable or under development and invite them to enquire about a free pilot. Keep answers practical, friendly and brief. If the visitor asks several detailed follow-up questions, or seems to want course-specific advice, pilot planning, or next-step discussion, gently direct them to use the contact form. If the visitor asks about an unrelated topic, politely explain that you can answer questions about MCQ Anchor. When mentioning short code snippets, commands, keys, literals, filenames, or syntax examples, wrap them in single backticks. Use fenced code blocks for multi-line code examples.
""".strip()

PRODUCT_KNOWLEDGE = """
MCQ Anchor is an educational assessment platform in development as a pilot product. It addresses a practical problem in higher education: online MCQs are useful for regular learning, but generative AI can now answer many conventional questions, so unsupervised online marks can be harder to trust. MCQ Anchor does not frame students as dishonest and does not rely on surveillance-style monitoring.

The core model is Learn, Practise, Validate, Calibrate.

Learn and Practise:
- AI creates candidate MCQs from approved text-based course materials.
- Questions are mapped to topics, learning outcomes and difficulty levels.
- Students complete regular online MCQs in their own time.
- Online practice includes immediate feedback, explanations, accuracy tracking, coverage tracking, sustained engagement tracking, target completion tracking, weak-area identification and multiple difficulty levels.
- Practice is for learning, so students may use notes, resources or generative AI during online practice if they choose.
- LTI 1.3 enabled. Integrate into your VLE seamlessly or use it as a standalone product.

Validate:
- Students book a short controlled paper-based MCQ validation session, typically around 15 to 20 minutes.
- Each student receives an unseen paper sampled from a secure validation pool.
- Validation questions assess the same learning outcomes and difficulty blueprint as practice questions.
- The paper is completed under controlled conditions without notes, phones or generative AI.
- Before booked sessions, staff print the relevant personalised student papers and answer sheets in advance so each validation is ready to run smoothly.
- Personalised QR-coded answer sheets are pre-populated with student details.
- Answer sheets can be photographed on a phone or tablet.
- Optical mark recognition processes responses and low-confidence scans are sent for manual review.
- The validation test provides a credible baseline of unaided knowledge.

Question design:
- Practice and validation use separate question pools.
- The practice pool is visible to students, supports learning, can repeat questions where helpful, and includes feedback and explanations.
- The secure validation pool is used only for controlled tests, is never released before a test, is quality assured, and is mapped to the same curriculum outcomes and difficulty blueprint.

Scoring:
- Scoring is configurable by the institution.
- An illustrative model is 80 percent online practice and 20 percent controlled validation.
- An illustrative online-practice breakdown is 40 percent accuracy and mastery, 30 percent curriculum coverage, 20 percent sustained engagement, and 10 percent target completion.
- An illustrative calibration rule is that the online-practice score cannot sit more than 20 percentage points above the controlled validation score.
- In plain English, strong online performance is rewarded when it is supported by validation.
- Institutions may also configure validation pass gates, reassessment rules, and low-practice rescue rules.

Accessibility and neurodiversity:
- MCQ Anchor is intended to support flexible regular practice rather than concentrating everything into one long high-stakes exam.
- Online practice can be completed in a familiar environment and at times set by the course, which may help some students manage pace, confidence and preparation.
- The controlled validation step is short compared with a traditional exam and is designed to check independent knowledge without requiring extended invigilation.
- The model may be particularly helpful for some neurodiverse students because it values repeated practice, predictable formats and a smaller validation event rather than relying only on one long final sitting.
- Accessibility arrangements and support should still be configurable by the institution, and any pilot should review how adjustments would work for specific student needs.
- MCQ Anchor should be discussed as a potentially more inclusive assessment model, not as a guarantee that every accessibility need is automatically solved.

Pilot invitation:
- MCQ Anchor is looking for educators who want to explore a fairer and more practical approach to MCQ assessment in the age of generative AI.
- Visitors can enquire about a free pilot through the website contact form.
""".strip()


def get_chatbot_reply(*, question: str, history: list[dict], api_key: str, model: str) -> str:
    client = OpenAI(api_key=api_key)
    input_items = [
        {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_INSTRUCTION}]},
        {"role": "system", "content": [{"type": "input_text", "text": PRODUCT_KNOWLEDGE}]},
    ]

    for item in history:
        content_type = "output_text" if item["role"] == "assistant" else "input_text"
        input_items.append(
            {
                "role": item["role"],
                "content": [{"type": content_type, "text": item["content"]}],
            }
        )

    input_items.append({"role": "user", "content": [{"type": "input_text", "text": question}]})

    try:
        response = client.responses.create(model=model, input=input_items)
    except Exception as exc:
        raise ChatbotError("OpenAI request failed") from exc

    text = (getattr(response, "output_text", "") or "").strip()
    if not text:
        raise ChatbotError("Empty chatbot response")
    return text
