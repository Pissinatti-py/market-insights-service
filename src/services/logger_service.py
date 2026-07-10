import logging
import sys

# ANSI escape codes for colors
LOG_COLORS = {
    "DEBUG": "\033[34m",  # Blue
    "INFO": "\033[38;5;191m",  # Yellow-green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bright Red
}
RESET_COLOR = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """
    Custom formatter to add colors based on log level.
    """

    def format(self, record):
        """
        Format a log record, wrapping it in the ANSI color for its level.

        :param record: The log record to format.
        :type record: logging.LogRecord
        :return: The color-wrapped formatted message.
        :rtype: str
        """
        log_color = LOG_COLORS.get(record.levelname, "")
        reset = RESET_COLOR

        formatted_message = super().format(record)
        return f"{log_color}{formatted_message}{reset}"


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger with the specified name, configured with a colored formatter.

    :param name: The name of the logger.
    :type name: str
    :return: A configured logger instance.
    :rtype: logging.Logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    formatter = ColoredFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger


logger = get_logger("market_insights")
