from decimal import Decimal

from invoice_tool.models import OcrToken
from invoice_tool.parser import _column_boundaries, _parse_item


def token(text: str, x0: float, x1: float, y: float = 10) -> OcrToken:
    return OcrToken(text, x0, y, x1, y + 8)


def test_wrapped_project_and_specification_use_their_own_columns() -> None:
    header = [
        token("项目名称", 45, 81),
        token("规格型号", 119, 155),
        token("单", 190, 199),
        token("位", 208, 217),
    ]
    item = _parse_item(
        [
            token("*敏感元件及传感器*传感", 13, 112, 20),
            token("CHVS-", 119, 142, 20),
            token("个", 198, 207, 20),
            token("2", 286, 291, 20),
            token("60.62", 298, 334, 20),
            token("121.24", 407, 434, 20),
            token("13%", 465, 479, 20),
            token("15.76", 560, 583, 20),
        ],
        [[token("器", 13, 22, 33), token("ASV-600VP202", 119, 173, 33)]],
        595,
        _column_boundaries(header, 595),
    )

    assert item.project_name == "*敏感元件及传感器*传感器"
    assert item.specification == "CHVS-ASV-600VP202"
    assert item.quantity == Decimal("2")
