"""Stock levels by item name."""


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def add(self, name: str, quantity: int) -> None:
        if quantity < 0:
            raise ValueError("quantity must be >= 0")
        self._stock[name] = self._stock.get(name, 0) + quantity

    def remove(self, name: str, quantity: int) -> None:
        have = self._stock.get(name, 0)
        if quantity > have:
            raise ValueError(f"only {have} {name} in stock")
        self._stock[name] = have - quantity

    def quantity(self, name: str) -> int:
        return self._stock.get(name, 0)

    def items(self) -> list[tuple[str, int]]:
        return sorted(self._stock.items())
