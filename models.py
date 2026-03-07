
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
# Pydantic Model


class Category(str, Enum):
    ORDER = "Order"
    OTHERS = "Others"

class AnnouncementType(BaseModel):
    category: Category = Field(..., 
                               description="Category of announcement.")
    justification: str = Field(..., description="Brief 1-sentence explanation for the classification.")

class PartnershipType(str, Enum):
    SOLO = "solo"
    JV = "joint_venture"
    CONSORTIUM = "consortium"

class PartnershipInfo(BaseModel):
    type: PartnershipType = Field(
        PartnershipType.SOLO, 
        description="Indicates if the project was won solo, as a Joint Venture, or a Consortium."
    )
    partners: List[str] = Field(
        default_factory=list,
        description="A list of other companies involved in the JV or Consortium. Leave empty if solo."
    )
    company_share_percent: Optional[float] = Field(
        None, 
        description="The percentage share of the current company in the JV/Consortium (e.g., 60.5)."
    )

class AwardStatus(str, Enum):
    AWARDED = "awarded"
    FIRST_LOWER = "first_lower"

class ProjectMarket(str, Enum):
    DOMESTIC = "domestic"
    INTERNATIONAL = "international"

class ProjectLocation(BaseModel):
    market: ProjectMarket = Field(
        ..., 
        description="'domestic' if the project is in India, otherwise 'international'."
    )
    country: str = Field(
        "India", 
        description="The specific country where the project is located (e.g., 'India', 'UAE', 'USA')."
    )
    state_or_region: Optional[str] = Field(
        None, 
        description="The specific state, province, or city mentioned (e.g., 'Maharashtra', 'Dubai')."
    )

class AmountScale(str, Enum):
    NONE = "none" # For exact numbers like 50000
    CRORE = "Crore"
    LAKH = "Lakh"
    THOUSAND = "Thousand"
    MILLION = "Million"
    BILLION = "Billion"

class AmountValue(BaseModel):
    raw_number: Optional[float] = Field(
        None, 
        description="The numerical part of the amount (e.g., 120 in '120 crores' or 50000 in '50,000')."
    )
    scale: AmountScale = Field(
        AmountScale.NONE,
        description="The unit/scale used in the text (e.g., Crore, Lakh, Million)."
    )
    currency: Optional[str] = Field(
        "INR", 
        description="The ISO 4217 currency code (e.g., 'INR', 'USD'). Default to INR if not specified."
    )
    is_per_year: bool = Field(
        False,
        description="True if the amount is specified as an annual figure (e.g., 'per annum', 'per year')."
    )

class TimeUnit(str, Enum):
    DAYS = "Days"
    WEEKS = "Weeks"
    MONTHS = "Months"
    YEARS = "Years"

class DurationValue(BaseModel):
    value: Optional[float] = Field(
        None, 
        description="The numerical length of the duration (e.g., 12, 1.5)."
    )
    unit: Optional[TimeUnit] = Field(
        None, 
        description="The unit of time for the duration."
    )

class DeadlineValue(BaseModel):
    iso_date: Optional[str] = Field(
        None, 
        description="The deadline formatted as an ISO 8601 date (YYYY-MM-DD)."
    )
    is_approximate: Optional[bool] = Field(
        False, 
        description="Set to true if the deadline is a rough estimate (e.g., 'Late 2025')."
    )
    original_text: Optional[str] = Field(
        None, 
        description="The exact phrase from the text referring to the deadline."
    )

class AnnouncementModel(BaseModel):
    awarding_entity: Optional[str] = Field(
        default="not specified", 
        description=(
            "The name of the company that issued the contract or award."
            "Use 'not specified' if the name is simply missing/ not mentioned/ confidential."
        )
    )
    
    award_status: Optional[AwardStatus] = Field(
        None,
        description="The status of the award (awarded or first_lower)."
    )

    partnership_details: Optional[PartnershipInfo] = Field(
        default_factory=lambda: PartnershipInfo(type=PartnershipType.SOLO),
        description="Details regarding whether the award was won solo or with partners."
    )

    total_order_value: Optional[AmountValue] = Field(
        None, 
        description="The structured monetary value and currency of the total order."
    )

    order_value_for_current_company: Optional[AmountValue] = Field(
        None, 
        description="The structured monetary value and currency of the order for the current company."
    )
    
    work_description: Optional[str] = Field(
        None, 
        description="Detailed scope of work exactly as per the announcement"
    )

    location: ProjectLocation = Field(
        ...,
        description="Geographical details of where the work is being performed."
    )

    project_duration: Optional[DurationValue] = Field(
        None, 
        description="The length of time the project is expected to take (e.g., '6 months' > value-6, unit-months, '2 years' > value-2, unit-years)."
    )
    
    project_deadline: Optional[DeadlineValue] = Field(
        None, 
        description="The structured completion date or final milestone."
    )

    extraction_evidence: List[str] = Field(
        ..., 
        description="A list of exact snippets from the text supporting the extracted data."
    )
    
class GICSResponse(BaseModel):
    sub_industry_name: str = Field(..., description="The name of the GICS Sub-Industry selected.")
    confidence_score: float = Field(..., ge=0, le=1.0)
    reasoning: str = Field(..., description="Short justification for this specific classification.")
