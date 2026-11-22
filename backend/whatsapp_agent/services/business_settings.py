from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.models import BusinessSettings


async def get_business_settings(session: AsyncSession) -> BusinessSettings:
    business = await session.get(BusinessSettings, 1)
    if business is not None:
        if not business.wholesale_group_jids and business.wholesale_group_jid:
            business.wholesale_group_jids = [business.wholesale_group_jid]
        elif business.wholesale_group_jids and not business.wholesale_group_jid:
            business.wholesale_group_jid = business.wholesale_group_jids[0]
        return business

    environment = get_settings()
    wholesale_group_jids = [
        jid.strip()
        for jid in environment.wholesale_group_jids.split(",")
        if jid.strip()
    ]
    if (
        environment.wholesale_group_jid
        and environment.wholesale_group_jid not in wholesale_group_jids
    ):
        wholesale_group_jids.append(environment.wholesale_group_jid)
    business = BusinessSettings(
        id=1,
        wholesale_group_jid=(
            wholesale_group_jids[0] if wholesale_group_jids else None
        ),
        wholesale_group_jids=wholesale_group_jids,
        retail_group_jid=environment.retail_group_jid,
        mum_admin_jid=environment.mum_admin_jid,
        wholesaler_jids=[
            jid.strip()
            for jid in environment.wholesaler_jids.split(",")
            if jid.strip()
        ],
        markup_rule=environment.markup_rule,
        min_profit_rule=environment.min_profit_rule,
    )
    session.add(business)
    await session.flush()
    return business
