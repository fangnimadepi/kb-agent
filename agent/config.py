from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # kb-search server 需要透传给子进程的平台配置
    kb_api_base: str = "http://127.0.0.1:8000"
    kb_access_token: str = ""

    # 工单库
    ticket_db_path: str = "data/tickets.db"

    # Agent 行为
    max_tool_iterations: int = 6  # 工具调用循环上限，防止无限循环
    max_reflect_retries: int = 2  # 反思重试上限（特赞 JD"错误兜底与重试机制"）


settings = Settings()
