from sqlalchemy import Column, Integer, Date
from sqlalchemy.orm import relationship

from .base import Base
from .table import GetByNameMixin


class Night(Base, GetByNameMixin):
    __tablename__ = 'pytel_night'

    id = Column(Integer, comment='Unique ID of night', primary_key=True)
    night = Column(Date, comment='Date at beginning  of night', unique=True, nullable=False)
    observations = relationship("Observation")

    def __init__(self, night=None):
        self.night = night


__all__= ['Night']