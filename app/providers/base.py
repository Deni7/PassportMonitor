from abc import ABC, abstractmethod
from datetime import date

from app.domain import Department, Location, Service, Slot, official_url


class QueueProvider(ABC):
    name: str
    date_window_days = 31

    @abstractmethod
    async def get_locations(self) -> list[Location]: ...

    @abstractmethod
    async def get_departments(self, location: Location) -> list[Department]: ...

    @abstractmethod
    async def get_services(self, department: Department) -> list[Service]: ...

    @abstractmethod
    async def get_available_dates(
        self, department: Department, service: Service, start: date, end: date
    ) -> list[date]: ...

    @abstractmethod
    async def get_available_slots(
        self, department: Department, service: Service, day: date
    ) -> list[Slot]: ...

    async def get_booking_url(self, department: Department, service: Service) -> str:
        return official_url(self.name, department.booking_url)

    async def close(self):
        return None
