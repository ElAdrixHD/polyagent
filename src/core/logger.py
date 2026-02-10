import logging
import sys

from colorama import Fore, Style, init

init(autoreset=True)

_LEVEL_COLORS = {
    logging.DEBUG: Fore.CYAN,
    logging.INFO: Fore.GREEN,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
}


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, "")
        ts = self.formatTime(record, "%H:%M:%S")
        level = record.levelname.ljust(8)
        return f"{Fore.WHITE}[{ts}] {color}[{level}]{Style.RESET_ALL} {record.getMessage()}"


def setup_logger(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("polyagent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ColorFormatter())
        logger.addHandler(handler)
    return logger


def format_opportunities_table(opportunities: list) -> str:
    if not opportunities:
        return "  No arbitrage opportunities found."
    header = (
        f"  {'#':<4} {'Profit':>7} {'YES':>6} {'NO':>6} {'Question':<60}"
    )
    sep = "  " + "-" * 85
    lines = [sep, header, sep]
    for i, opp in enumerate(opportunities, 1):
        q = opp.question[:57] + "..." if len(opp.question) > 60 else opp.question
        lines.append(
            f"  {i:<4} {opp.profit:>+6.1%} {opp.yes_price:>6.3f} {opp.no_price:>6.3f} {q:<60}"
        )
    lines.append(sep)
    return "\n".join(lines)
