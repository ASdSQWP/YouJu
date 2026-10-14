"""一次回答里的来源编号。同一个切片无论被检索几次，编号都不变。"""

from __future__ import annotations

from youju.textproc import tokens


class SourceBook:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self._by_chunk: dict[int, int] = {}
        self._tokens: dict[int, frozenset[str]] = {}

    def __len__(self) -> int:
        return len(self.items)

    def add(self, hit) -> int:
        if hit.chunk_id in self._by_chunk:
            return self._by_chunk[hit.chunk_id]
        number = len(self.items) + 1
        item = hit.public()
        item["n"] = number
        self.items.append(item)
        self._by_chunk[hit.chunk_id] = number
        self._tokens[number] = frozenset(tokens(f"{hit.section}\n{hit.text}"))
        return number

    def numbers(self) -> set[int]:
        return set(self._tokens)

    def get(self, number: int) -> dict | None:
        return self.items[number - 1] if 1 <= number <= len(self.items) else None

    def token_set(self, number: int) -> frozenset[str]:
        return self._tokens.get(number, frozenset())

    def context(self, numbers: list[int] | None = None, limit: int = 900) -> str:
        chosen = self.items if numbers is None else [self.items[n - 1] for n in numbers if 1 <= n <= len(self.items)]
        blocks = []
        for item in chosen:
            where = item["filename"]
            if item.get("section"):
                where += f" · {item['section']}"
            if item.get("page"):
                where += f" · 第 {item['page']} 页"
            text = item["text"] if len(item["text"]) <= limit else item["text"][:limit] + "…"
            blocks.append(f"[{item['n']}] 《{where}》\n{text}")
        return "\n\n".join(blocks)
