import json
import logging
import os
import re
import time
from datetime import timedelta
from typing import Any
from dotenv import load_dotenv
from langchain.chains import create_history_aware_retriever
from langchain_community.chat_message_histories import MomentoChatMessageHistory
from langchain_community.vectorstores import Pinecone
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_bolt.adapter.socket_mode import SocketModeHandler

CHAT_UPDATE_INTERVAL_SEC = 1

load_dotenv()

# 로그
SlackRequestHandler.clear_all_log_handlers()
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# 봇 토큰과 소켓 모드 핸들러를 사용하여 앱을 초기화
app = App(
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    token=os.environ["SLACK_BOT_TOKEN"],
    process_before_response=True,
)

class SlackStreamingCallbackHandler(BaseCallbackHandler):
    last_send_time = time.time()
    message = ""

    def __init__(self, channel, ts):
        self.channel = channel
        self.ts = ts
        self.interval = CHAT_UPDATE_INTERVAL_SEC
        # 게시글을 업데이트한 누적 횟수 카운터
        self.update_count = 0

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        self.message += token
        now = time.time()
        if now - self.last_send_time > self.interval:
            app.client.chat_update(
                channel=self.channel,
                ts=self.ts,
                text=f"{self.message}\n\nTyping...",
            )
            self.last_send_time = now
            self.update_count += 1
            # update_count가 현재의 업데이트 간격 X10보다 많아질 때마다 업데이트 간격을 2배로 늘림
            if self.update_count / 10 > self.interval:
                self.interval = self.interval * 2

    def on_llm_end(self, response, **kwargs: Any) -> Any:
        app.client.chat_update(
            channel=self.channel,
            ts=self.ts,
            text=self.message,
        )

def initialize_vectorstore():
    from langchain_openai import OpenAIEmbeddings
    index_name = os.environ["PINECONE_INDEX"]
    embeddings = OpenAIEmbeddings()
    return Pinecone.from_existing_index(index_name, embeddings)

@app.event("app_mention")
def handle_mention(event, say):
    channel = event["channel"]
    thread_ts = event["ts"]
    message = re.sub("<@.*>", "", event["text"])

    # 게시글 키(=Momento 키): 첫 번째=event["ts"], 두 번째 이후=event["thread_ts"]
    id_ts = event["ts"]
    if "thread_ts" in event:
        id_ts = event["thread_ts"]

    result = say("\n\nTyping...", thread_ts=thread_ts)
    ts = result["ts"]

    history = MomentoChatMessageHistory.from_client_params(
        id_ts,
        os.environ["MOMENTO_CACHE"],
        timedelta(hours=int(os.environ["MOMENTO_TTL"]))
    )

    vectorstore = initialize_vectorstore()
    retriever = vectorstore.as_retriever()
    rephrase_prompt = ChatPromptTemplate.from_messages(
        [
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}"),
            ("user", "위의 대화 내용을 바탕으로 적합한 검색 쿼리를 작성해 주세요."),
        ]
    )
    rephrase_llm = ChatOpenAI(
        model_name=os.environ["OPENAI_API_MODEL"],
        temperature=os.environ["OPENAI_API_TEMPERATURE"],
    )
    rephrase_chain = create_history_aware_retriever(rephrase_llm, retriever, rephrase_prompt)

    callback = SlackStreamingCallbackHandler(channel=channel, ts=ts)
    qa_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "아래 문맥만을 참고하여 답변해 주세요.\n\n{context}"),
            MessagesPlaceholder(variable_name="chat_history"),
            ("user", "{input}"),
        ]
    )
    qa_llm = ChatOpenAI(
        model_name=os.environ["OPENAI_API_MODEL"],
        temperature=os.environ["OPENAI_API_TEMPERATURE"],
        streaming=True,
        callbacks=[callback],
    )
    qa_chain = qa_prompt | qa_llm | StrOutputParser()

    conversational_chain = (
        qa_chain | rephrase_chain
    )

    ai_message = conversational_chain.invoke(
        {"input": message, "chat_history": history.messages}
    )

    history.add_user_message(message)
    history.add_ai_message(ai_message)

# 소켓 모드 핸들러를 사용해 앱을 시작
if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

def handler(event, context):
    logger.info("handler called")
    header = event["headers"]
    if "x-slack-retry-num" in header:
        return 200
 
    # AWS Lambda 환경의 요청 정보를 앱이 처리할 수 있도록 변환해 주는 어댑터
    slack_handler = SlackRequestHandler(app=app)
    # 응답을 그대로 AWS Lambda의 반환 값으로 반환할 수 있다
    return slack_handler.handle(event, context)