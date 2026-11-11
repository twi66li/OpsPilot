"""兼容已有logging日志的JSON输出，并为HTTP请求绑定关联编号。"""
import logging

import structlog


def configure_logging():
    processors = [structlog.contextvars.merge_contextvars, structlog.stdlib.add_log_level,
                  structlog.stdlib.add_logger_name, structlog.processors.TimeStamper(fmt="iso", utc=True)]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info, structlog.processors.JSONRenderer(ensure_ascii=False)],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    structlog.configure(processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
                        logger_factory=structlog.stdlib.LoggerFactory(),
                        wrapper_class=structlog.stdlib.BoundLogger, cache_logger_on_first_use=True)
