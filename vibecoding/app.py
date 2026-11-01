from io import BytesIO
import hashlib
import json
from datetime import datetime

from openai import OpenAI
from supabase import Client, create_client
import streamlit as st
from pypdf import PdfReader


QUIZ_SYSTEM_PROMPT = """
당신은 학습 퀴즈를 만드는 도우미입니다.
주어진 요약을 바탕으로 4지선다 퀴즈 3문항을 만드세요.
각 문항은 question, options(4개), answer(정답 인덱스, 0부터 시작), explanation을 포함해야 합니다.
결과는 {"questions": [...]} 형태의 JSON으로 반환하세요.
"""


st.set_page_config(
    page_title="AI 학습 정리 노트",
    page_icon="📚",
    layout="wide",
)

st.title("AI 학습 정리 노트")
st.write("PDF를 업로드하면 문서의 텍스트를 바로 확인할 수 있습니다.")
st.session_state.setdefault("summary", None)
st.session_state.setdefault("summary_source", None)
st.session_state.setdefault("saved_summary_id", None)
st.session_state.setdefault("quiz", None)
st.session_state.setdefault("quiz_source", None)
st.session_state.setdefault("quiz_persisted", False)
summary_table_names = ("summaries", "sumarries")


@st.cache_resource
def get_supabase_client() -> Client:
    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_KEY"],
    )


@st.cache_resource
def get_summary_table_name() -> str:
    client = get_supabase_client()
    for table_name in summary_table_names:
        try:
            client.table(table_name).select("id").limit(1).execute()
            return table_name
        except Exception:
            continue
    raise RuntimeError("summaries 테이블을 찾을 수 없습니다.")


def save_summary(filename: str, summary: dict) -> int:
    client = get_supabase_client()
    response = client.table(get_summary_table_name()).insert(
        {
            "filename": filename,
            "summary": summary,
            "quiz": {},
        }
    ).execute()
    return response.data[0]["id"]


def update_summary_quiz(summary_id: int, quiz: dict) -> None:
    client = get_supabase_client()
    client.table(get_summary_table_name()).update(
        {"quiz": quiz}
    ).eq("id", summary_id).execute()


def load_summaries() -> list[dict]:
    client = get_supabase_client()
    response = (
        client.table(get_summary_table_name())
        .select("id, filename, summary, created_at")
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


def delete_summary(summary_id: int) -> None:
    client = get_supabase_client()
    client.table(get_summary_table_name()).delete().eq("id", summary_id).execute()


def format_created_at(created_at: str | None) -> str:
    if not created_at:
        return "-"
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return created_at


def render_history_tab() -> None:
    st.subheader("저장 기록")
    try:
        records = load_summaries()
    except KeyError:
        st.error("Supabase 설정이 없습니다. .streamlit/secrets.toml을 확인해 주세요.")
        return
    except Exception as error:
        st.error(f"저장 기록을 불러오는 중 문제가 발생했습니다: {error}")
        return

    if not records:
        st.info("저장된 요약이 없습니다.")
        return

    header_columns = st.columns([2, 2, 5, 1])
    header_columns[0].caption("파일명")
    header_columns[1].caption("저장 시각")
    header_columns[2].caption("핵심 요약")

    for record in records:
        columns = st.columns([2, 2, 5, 1])
        columns[0].write(record.get("filename", "-"))
        columns[1].write(format_created_at(record.get("created_at")))

        summary = record.get("summary") or {}
        core_summary = summary.get("핵심 요약", "-") if isinstance(summary, dict) else "-"
        core_summary = str(core_summary)
        if len(core_summary) > 120:
            core_summary = f"{core_summary[:120]}..."
        columns[2].write(core_summary)

        if columns[3].button("삭제", key=f"delete_summary_{record['id']}"):
            try:
                delete_summary(record["id"])
                st.rerun()
            except Exception as error:
                st.error(f"저장 기록을 삭제하는 중 문제가 발생했습니다: {error}")

with st.sidebar:
    st.header("문서 업로드")
    uploaded_file = st.file_uploader(
        "PDF 파일 업로드",
        type=["pdf"],
        accept_multiple_files=False,
    )

    st.divider()
    st.subheader("문서 정보")
    if uploaded_file is None:
        st.caption("업로드된 문서가 없습니다.")
    else:
        st.write(f"**파일명**  \n{uploaded_file.name}")

original_tab, summary_tab, quiz_tab, history_tab = st.tabs(
    ["원문", "요약", "퀴즈", "저장 기록"]
)

if uploaded_file is None:
    with original_tab:
        st.info("사이드바에서 학습할 PDF 파일을 선택해 주세요.")
    with summary_tab:
        st.info("PDF를 업로드하면 요약할 수 있습니다.")
    with quiz_tab:
        st.info("PDF 요약을 먼저 만들어 주세요.")
else:
    try:
        file_bytes = uploaded_file.getvalue()
        source_id = hashlib.sha256(file_bytes).hexdigest()
        if st.session_state.summary_source != source_id:
            st.session_state.summary = None
            st.session_state.summary_source = source_id
            st.session_state.saved_summary_id = None
            st.session_state.quiz = None
            st.session_state.quiz_source = None
            st.session_state.quiz_persisted = False

        reader = PdfReader(BytesIO(file_bytes))
        page_count = len(reader.pages)
        with st.sidebar:
            st.write(f"**총 페이지 수**  \n{page_count}페이지")

        page_texts = [
            (page.extract_text() or "").strip()
            for page in reader.pages
        ]
        with original_tab:
            st.subheader("추출된 텍스트")
            for page_number, extracted_text in enumerate(page_texts, start=1):

                with st.expander(f"{page_number}페이지", expanded=page_number == 1):
                    if extracted_text:
                        st.text_area(
                            "PDF 내용",
                            value=extracted_text,
                            height=420,
                            label_visibility="collapsed",
                            key=f"page_text_{page_number}",
                        )
                    else:
                        st.warning("텍스트를 추출할 수 없는 PDF입니다. 이미지 기반 PDF일 수 있습니다.")

        with summary_tab:
            st.subheader("PDF 요약")
            if st.button("요약하기", type="primary"):
                try:
                    api_key = st.secrets["OPENAI_API_KEY"]
                    client = OpenAI(api_key=api_key)
                    document_text = "\n\n".join(
                        f"[페이지 {page_number}]\n{text}"
                        for page_number, text in enumerate(page_texts, start=1)
                        if text
                    )

                    if not document_text:
                        st.warning("요약할 텍스트가 없습니다. 이미지 기반 PDF일 수 있습니다.")
                    else:
                        with st.spinner("PDF를 요약하는 중입니다..."):
                            response = client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=[
                                    {
                                        "role": "system",
                                        "content": (
                                            "다음 PDF 텍스트를 분석하고, 핵심 요약(3문장), "
                                            "주요 키워드(5개), 난이도(상/중/하)를 JSON으로 반환하세요. "
                                            "JSON 키는 '핵심 요약', '주요 키워드', '난이도'를 사용하세요."
                                        ),
                                    },
                                    {"role": "user", "content": document_text},
                                ],
                                response_format={"type": "json_object"},
                            )
                            st.session_state.summary = json.loads(
                                response.choices[0].message.content or "{}"
                            )

                except KeyError:
                    st.error("OpenAI API 키가 설정되지 않았습니다. .streamlit/secrets.toml을 확인해 주세요.")
                except (json.JSONDecodeError, IndexError, TypeError) as error:
                    st.error(f"요약 결과를 처리하는 중 문제가 발생했습니다: {error}")
                except Exception as error:
                    st.error(f"PDF 요약 중 문제가 발생했습니다: {error}")

            if st.session_state.summary:
                summary = st.session_state.summary
                st.markdown("**핵심 요약**")
                st.write(summary.get("핵심 요약", "요약 결과가 없습니다."))
                st.markdown("**주요 키워드**")
                keywords = summary.get("주요 키워드", [])
                st.write(", ".join(keywords) if isinstance(keywords, list) else keywords)
                st.markdown("**난이도**")
                st.write(summary.get("난이도", "정보 없음"))

                if st.button("저장"):
                    try:
                        st.session_state.saved_summary_id = save_summary(
                            uploaded_file.name,
                            summary,
                        )
                        st.success("요약 결과를 저장했습니다.")
                    except KeyError:
                        st.error("Supabase 설정이 없습니다. .streamlit/secrets.toml을 확인해 주세요.")
                    except Exception as error:
                        st.error(f"요약 결과를 저장하는 중 문제가 발생했습니다: {error}")

        with quiz_tab:
            st.subheader("학습 퀴즈")
            if not st.session_state.summary:
                st.info("요약을 먼저 만들어 주세요.")
            else:
                if st.button("퀴즈 생성", type="primary"):
                    try:
                        api_key = st.secrets["OPENAI_API_KEY"]
                        client = OpenAI(api_key=api_key)
                        summary_text = json.dumps(
                            st.session_state.summary,
                            ensure_ascii=False,
                        )
                        with st.spinner("퀴즈를 만드는 중입니다..."):
                            response = client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=[
                                    {"role": "system", "content": QUIZ_SYSTEM_PROMPT},
                                    {"role": "user", "content": summary_text},
                                ],
                                response_format={"type": "json_object"},
                            )
                            quiz = json.loads(
                                response.choices[0].message.content or "{}"
                            )
                            questions = quiz.get("questions", [])
                            if not isinstance(questions, list) or len(questions) != 3:
                                raise ValueError("퀴즈 3문항 형식이 아닙니다.")
                            st.session_state.quiz = quiz
                            st.session_state.quiz_source = source_id
                            st.session_state.quiz_persisted = False

                        if st.session_state.saved_summary_id is None:
                            st.warning("먼저 저장해주세요.")
                        else:
                            update_summary_quiz(
                                st.session_state.saved_summary_id,
                                quiz,
                            )
                            st.session_state.quiz_persisted = True
                            st.success("퀴즈를 저장된 요약에 반영했습니다.")
                    except KeyError:
                        st.error("OpenAI API 키가 설정되지 않았습니다. .streamlit/secrets.toml을 확인해 주세요.")
                    except (json.JSONDecodeError, IndexError, TypeError, ValueError) as error:
                        st.error(f"퀴즈 결과를 처리하는 중 문제가 발생했습니다: {error}")
                    except Exception as error:
                        st.error(f"퀴즈 생성 중 문제가 발생했습니다: {error}")

                if st.session_state.quiz:
                    if not st.session_state.quiz_persisted:
                        st.warning("먼저 저장해주세요.")
                    for question_number, question in enumerate(
                        st.session_state.quiz.get("questions", []),
                        start=1,
                    ):
                        st.markdown(
                            f"**{question_number}. {question.get('question', '질문 없음')}**"
                        )
                        options = question.get("options", [])
                        option_labels = [
                            f"{option_number}. {option}"
                            for option_number, option in enumerate(options, start=1)
                        ]
                        selected_option = st.radio(
                            "보기",
                            option_labels,
                            index=None,
                            key=f"quiz_answer_{st.session_state.summary_source}_{question_number}",
                            label_visibility="collapsed",
                        )
                        if selected_option is not None:
                            selected_index = option_labels.index(selected_option)
                            answer_index = question.get("answer")
                            if selected_index == answer_index:
                                st.success("정답입니다!")
                            else:
                                st.error("오답입니다.")
                            st.info(question.get("explanation", "해설이 없습니다."))
    except Exception as error:
        st.error(f"PDF를 읽는 중 문제가 발생했습니다: {error}")

with history_tab:
    render_history_tab()
