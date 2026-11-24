# Korea's bank hack was cheap, not clever. That is the news.

*LinkedIn article draft, 9 October 2026. Working length about 1,500 words. Every number below is traced in the companion research report; the report's own caveat applies: the figures were gathered from search extracts of the cited pages, so open each link before publishing.*

---

Last week an attacker pulled personal data on more than 68,000 people out of seven Korean financial institutions. The viral version of the story says a 26-year-old hacked nine banks with Claude Code. The record says something less dramatic and more alarming.

The attacker did not break core banking. Nobody's password, one-time code or money was taken. What was taken came out of side doors: a mobile portal for loan agents at Shinhan, an employee work-support system at KB Kookmin, a sales-support database at Hana, a broker query page at Hyundai Capital. These were internet-facing systems that used simplified authentication instead of the certificate-based login customers see, and they sat outside the routine vulnerability checks that the Electronic Financial Supervisory Regulations require. The attacker cycled identifiers through them at a low rate from rotating IP addresses until records came back. Shinhan's own timeline shows the intrusion starting at 18:04 on 28 September, detection at 09:30 the next morning, and the attacks continuing for roughly fifteen hours after detection.

CrowdStrike's report, published on 7 October, found the campaign by finding the attacker's mistakes. World-readable directories on a Hong Kong server exposed his Claude Code session histories and the configuration of ARTEX, an open-source agentic penetration-testing tool that first appeared on GitHub in July. ARTEX ran on DeepSeek v4.1-flash, bought through an API reseller. Claude, along with GLM and Grok sessions, was used for planning, generating commands, and asking where Korean breach data is usually sold. CrowdStrike's attribution is moderate confidence. Korean regulators have not adopted it; their language is still "AI possibly used," and the police are still deciding whether this was one person or a group. The Financial Security Institute, which traced the tool's fingerprints across three banks, put it precisely: the AI did not act independently without human involvement.

So the honest headline is this. An unattributed operator with a free tool, a budget model bought from a reseller, and a commercial chatbot for homework reached 68,000 people's data in a week, by exploiting weaknesses that Korea had already named in public four times.

## We have read this report before

The SK Telecom investigation found that the attackers planted their first backdoor in August 2021 on a management server holding plaintext credentials, reached the subscriber authentication server by Christmas of that year, and were discovered in April 2025. Three years and eight months. SKT's own review in February 2022 had found abnormal logins, checked one of six log records, and reported nothing. Twenty-three million people were affected.

The KT investigation found that a single manufacturer certificate, valid for ten years and shared by every femtocell on the network, let a home-made base station sit on the core network for eleven months. It also found that KT had discovered 43 infected servers in 2024, cleaned them quietly, and given the regulator false disposal dates.

The LG Uplus investigation found a default administrator password unchanged since 2018 and end-of-life software across the stack. In 2025 the company wiped a server after the ministry passed on a tip about it, and is now under criminal investigation for obstruction.

The Coupang investigation found that a former engineer had copied a signing key that was never rotated after he left, that the gateway did not properly verify the tokens he forged with it, and that he viewed delivery records 148 million times over seven months without tripping a threshold. About five months of logs were deleted despite a preservation order. Thirty-three million members, plus at least four million non-members.

Lay the five cases side by side and they share one anatomy: an asset nobody knew was exposed, a secret that was plaintext or never rotated, no segmentation or outbound monitoring, logs too short or destroyed, and reporting that came late or not at all. Every one of these companies held the ISMS-P certification. Coupang held seven certifications. Certification did not predict defence in a single case.

That is why I resist the framing that this is an AI story. AI changed the speed and the price of the attack. It did not change the door. The research underneath this piece looked at the 2026 threat reports from CrowdStrike, Mandiant, Microsoft and Verizon, and they agree with KISA's own analysts: the entry points are still exposed admin functions, weak authentication and unpatched flaws, and most firms do not know which of their systems face the internet. What agentic tooling did is collapse the skill, staffing and time needed to find those systems. The operator in this case never had to jailbreak anything. He used an open tool on open-weight models and asked the frontier model only for things that do not trip a safety filter. Provider-side safeguards will not be the backstop. Defenders should assume the next fork of ARTEX runs just as well.

## The law has already moved. The question is whether companies will.

Here I want to correct something I had wrong myself until I looked. I had been describing the Network Act's CISO reforms as something coming. They are here. The amended Information and Communications Network Act passed the National Assembly on 12 March and took effect on 1 October, four days after the bank attacks began.

It requires ICT service providers above the medium-enterprise threshold to appoint an executive as CISO. It gives the CISO statutory duties that include drawing up the security budget, managing security staff, and reporting security status to the board. It requires an information security committee chaired by the CISO, with the heads of development, privacy, HR and finance at the table, reporting to the CEO and the board. It lets the ministry investigate a suspected breach without waiting for the company to report it, which closes the gap KT exploited. And it adds a surcharge of up to 3 percent of related revenue for repeat incidents caused by intent or gross negligence. All three carriers now carry a first strike from 2025.

The privacy law moved too. The amended PIPA, in force since 11 September, raises the fine ceiling to 10 percent of total revenue for repeat or mega breaches, requires the board to resolve on the privacy officer's appointment, and makes reporting speed a direct factor in the penalty.

Finance, the sector just breached, is the laggard. The old 5-5-7 staffing and budget rule was replaced with principles in 2025. The bill that would make a bank's representative director ultimately responsible for security has sat in subcommittee since March. Five bank CEOs will be in front of the National Assembly on 19 October. That is the moment to move it.

But a title is not a budget. The disclosure data explains why I am cautious about what the new law will do on its own. Of the 773 companies that filed information security disclosures last year, 78 percent reported a CISO who holds another job. About 29 percent of those CISOs were not executives. The most common share of a CISO's time spent on security was between 5 and 10 percent. A survey of 396 CISOs found a quarter with no dedicated security staff at all. Korean companies put about 6.3 percent of IT investment into security. The US benchmark is near 11 percent, and the FSS governor has cited 13 percent at leading international financial institutions. In 2024 SK Telecom spent 60 billion won on security and 137 billion won on advertising.

A law can make the CISO an executive. It cannot make the executive heard.

## What Korean tech companies should do now

The order matters, and it starts with the thing the banks skipped.

**Know what faces the internet.** Every partner portal, loan-agent site, employee mobile tool, temporary server and femtocell. The FSC's emergency instruction on 4 October said exactly this: inventory externally exposed assets, close any path to internal data that skips authentication, and block external access unless operationally unavoidable. That instruction should not have needed a breach.

**Treat authentication on those portals as seriously as on the customer login.** Phishing-resistant MFA, object-level authorisation so that cycling identifiers returns nothing, and rate limits tuned for low-and-slow enumeration. The Shinhan traffic looked normal because the agent made few attempts per burst.

**Give every secret an expiry.** Coupang's signing key lived five to ten years and survived its owner's departure. KT's certificate lived ten years. The government cut KT's to one month. Use that ratio as a mental model. Rotate automatically, revoke on offboarding through HR, and keep keys out of laptops.

**Keep logs you cannot delete.** Three of the five cases ended with criminal referrals for log deletion. Immutable logging and a legal-hold procedure are now a liability control, not just a forensic one.

**Test the architecture, not the path.** Coupang's penetration tests fixed only the paths they tested and never looked at gateway token validation. KISA's October drill will use an AI agent as the attacker, and the national bug bounty logged 1,837 reports last year. Both are free ways to borrow the attacker's new tooling.

**Report inside the clock.** Under the new PIPA, reporting speed changes the fine. Under the new Network Act, quiet self-remediation is no longer an option because the ministry can come in uninvited.

For boards, the ask is simpler. Use the new law as a lever. Put the CISO under the CEO with direct board access. Separate the CISO and privacy officer roles where they are still one person. Recruit a director who can read a security report; SK Telecom's board had none. Protect the CISO's tenure. Then set a number for security's share of IT spend, publish it, and track execution, because the major banks' execution rate has fallen three years running even as budgets grew.

## What should happen locally

National law sets the ceiling. The floor gets built locally, and here Korea has supply without demand.

MSIT and KISA run eleven regional information security support centres that offer checks, consulting and solution subsidies to small and mid-sized firms. Regional clusters opened in Busan and Chungcheong. The SME ministry's technology-protection voucher covers up to 80 percent of costs. Incheon passed an industrial-security ordinance. Jeju launched a regional security council. The delivery network exists.

The uptake tells the other half. KISA's threat-sharing platform has enrolled 5,000 of 30,000 target firms, and SME participation sits at 0.03 percent, even though roughly 70 percent of ransomware victims are SMEs. Only 8.7 percent of firms with 50 or more staff say they need security personnel. Barely a third have a security organisation at all.

Local governments can create the demand the national programmes cannot. Three moves would do it. Require a baseline security mark in municipal and public-agency procurement, so that a small supplier has a commercial reason to get one; the UK's Cyber Essentials and Singapore's Cyber Trust mark show that a lightweight, tiered mark tied to procurement and insurance is what small firms actually adopt, where ISMS-P is too heavy to reach them. Route voucher money through the regional centres toward the hygiene floor above, not generic consulting. And run the KISA AI-agent drill for local firms in each cluster, with the regional universities that already produce security graduates supplying the people, so that trained graduates have a local market instead of leaving for Seoul or abroad.

## The reset

Korea has done something unusual. It legislated the governance half of the answer before the next incident rather than after it. The test of the next twelve months is whether an executive CISO and a board committee change how budgets are actually set, whether the courts uphold revenue-based fines so the 10 percent tier means anything, and whether the finance bill moves while five CEOs are in the room.

For Korean tech companies the Coupang accountability gap and the banks' unguarded side doors are one disease: security owned by nobody with authority, measured by certificates instead of exposure, and funded at a ratio that has not moved in three years. The reset starts when a board can answer three questions. What share of IT do we spend on security? Who reports to us about what faces the internet? How long has our longest-lived credential been alive?

The attacker already knows the answers. He just asked a chatbot.

---

*Andy Herman teaches information assurance and cybersecurity at the University of Washington Information School.*

---

## Notes for publishing

- **Verify before posting.** The companion report grades every claim into confirmed, reported and viral. The figures most worth opening the source for: the FSC total (68,000), the disclosure ratios (6.29 vs 6.44 percent), the Network Act effective date (MSIT says 1 October; one statute page shows 2 October), and the 78 percent dual-hatting figure.
- **Attribution language.** Keep "CrowdStrike attributes with moderate confidence" and "Korean authorities have not adopted the attribution." The résumé identity in the report is explicitly described by CrowdStrike as not definitively linked, and a man reached by Reuters denied involvement. Do not name him.
- **Headline alternatives.** "Korea has read this breach report four times already." "A title is not a budget: Korea's new CISO law meets its first test." "The attacker did not break the bank. He walked in the side door."
- **Cuts if over length.** The LG Uplus paragraph and the regional programme inventory can each lose a sentence without losing the argument.
- **Tagging.** FSC, FSS, KISA, MSIT and the Financial Security Institute are named; consider tagging the Korea CISO community (CONCERT) and the UW iSchool.
