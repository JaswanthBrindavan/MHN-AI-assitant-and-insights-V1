"""Localized deterministic safety replies.

DRAFT — pending clinician AND native-speaker sign-off. Only the deterministic
safety-critical strings are localized here; ordinary answers follow the user's
language via the LLM directive. Unknown language → English.
"""

from __future__ import annotations

from app.chat.replies import HIGH_ESCALATION, SCOPE_DECLINE
from app.triage.red_flags import EMERGENCY_DIRECTIVE

_EMERGENCY: dict[str, str] = {
    "en": EMERGENCY_DIRECTIVE,
    "hi": (
        "यह एक मेडिकल इमरजेंसी हो सकती है। कृपया तुरंत अपने स्थानीय आपातकालीन "
        "नंबर पर कॉल करें या नज़दीकी इमरजेंसी विभाग जाएँ। "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "hi-Latn": (
        "Yeh ek medical emergency ho sakti hai. Kripya turant apne local "
        "emergency number par call karein ya nazdeeki emergency department "
        "jayein. (This may be a medical emergency — please call your local "
        "emergency number or go to the nearest emergency department right now.)"
    ),
    "te": (
        "ఇది మెడికల్ ఎమర్జెన్సీ కావచ్చు. దయచేసి వెంటనే మీ లోకల్ ఎమర్జెన్సీ "
        "నంబర్‌కు కాల్ చేయండి లేదా దగ్గరి ఎమర్జెన్సీ విభాగానికి వెళ్లండి. "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "ta": (
        "இது ஒரு மருத்துவ அவசரநிலையாக இருக்கலாம். உடனே உங்கள் உள்ளூர் "
        "அவசர எண்ணை அழைக்கவும் அல்லது அருகிலுள்ள அவசர சிகிச்சைப் "
        "பிரிவுக்குச் செல்லவும். (This may be a medical emergency — please "
        "call your local emergency number or go to the nearest emergency "
        "department right now.)"
    ),
    "bn": (
        "এটি একটি মেডিকেল ইমার্জেন্সি হতে পারে। অনুগ্রহ করে এখনই আপনার "
        "স্থানীয় জরুরি নম্বরে কল করুন বা নিকটবর্তী জরুরি বিভাগে যান। "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "mr": (
        "ही वैद्यकीय आणीबाणी असू शकते. कृपया त्वरित आपल्या स्थानिक आपत्कालीन "
        "क्रमांकावर कॉल करा किंवा जवळच्या आपत्कालीन विभागात जा. "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "kn": (
        "ಇದು ವೈದ್ಯಕೀಯ ತುರ್ತುಸ್ಥಿತಿ ಇರಬಹುದು. ದಯವಿಟ್ಟು ತಕ್ಷಣ ನಿಮ್ಮ ಸ್ಥಳೀಯ "
        "ತುರ್ತು ಸಂಖ್ಯೆಗೆ ಕರೆ ಮಾಡಿ ಅಥವಾ ಹತ್ತಿರದ ತುರ್ತು ವಿಭಾಗಕ್ಕೆ ಹೋಗಿ. "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "ml": (
        "ഇത് ഒരു മെഡിക്കൽ എമർജൻസി ആയിരിക്കാം. ദയവായി ഉടൻ നിങ്ങളുടെ ലോക്കൽ "
        "എമർജൻസി നമ്പറിൽ വിളിക്കുക, അല്ലെങ്കിൽ അടുത്തുള്ള എമർജൻസി "
        "വിഭാഗത്തിലേക്ക് പോകുക. (This may be a medical emergency — please "
        "call your local emergency number or go to the nearest emergency "
        "department right now.)"
    ),
    "gu": (
        "આ મેડિકલ ઈમરજન્સી હોઈ શકે છે. કૃપા કરીને તરત જ તમારા સ્થાનિક "
        "ઈમરજન્સી નંબર પર કૉલ કરો અથવા નજીકના ઈમરજન્સી વિભાગમાં જાઓ. "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
    "pa": (
        "ਇਹ ਮੈਡੀਕਲ ਐਮਰਜੈਂਸੀ ਹੋ ਸਕਦੀ ਹੈ। ਕਿਰਪਾ ਕਰਕੇ ਤੁਰੰਤ ਆਪਣੇ ਲੋਕਲ "
        "ਐਮਰਜੈਂਸੀ ਨੰਬਰ ਤੇ ਕਾਲ ਕਰੋ ਜਾਂ ਨੇੜਲੇ ਐਮਰਜੈਂਸੀ ਵਿਭਾਗ ਵਿੱਚ ਜਾਓ। "
        "(This may be a medical emergency — please call your local emergency "
        "number or go to the nearest emergency department right now.)"
    ),
}

_HIGH: dict[str, str] = {
    "en": HIGH_ESCALATION,
    "hi": (
        "आपने जो बताया है वह गंभीर हो सकता है। कृपया बिना देर किए डॉक्टर या "
        "अर्जेंट केयर से संपर्क करें। (Some of what you describe can be serious "
        "— please seek medical care promptly.)"
    ),
    "hi-Latn": (
        "Aapne jo bataya hai woh serious ho sakta hai. Kripya bina der kiye "
        "doctor ya urgent care se sampark karein. (Some of what you describe "
        "can be serious — please seek medical care promptly.)"
    ),
    "te": (
        "మీరు చెప్పినది తీవ్రంగా ఉండవచ్చు. దయచేసి ఆలస్యం చేయకుండా డాక్టర్‌ను "
        "సంప్రదించండి. (Some of what you describe can be serious — please "
        "seek medical care promptly.)"
    ),
    "ta": (
        "நீங்கள் சொன்னது தீவிரமாக இருக்கலாம். தயவு செய்து தாமதிக்காமல் "
        "மருத்துவரை அணுகவும். (Some of what you describe can be serious — "
        "please seek medical care promptly.)"
    ),
    "bn": (
        "আপনি যা বলেছেন তা গুরুতর হতে পারে। অনুগ্রহ করে দেরি না করে "
        "ডাক্তারের সাথে যোগাযোগ করুন। (Some of what you describe can be "
        "serious — please seek medical care promptly.)"
    ),
    "mr": (
        "आपण सांगितलेले गंभीर असू शकते. कृपया उशीर न करता डॉक्टरांशी संपर्क "
        "साधा. (Some of what you describe can be serious — please seek "
        "medical care promptly.)"
    ),
    "kn": (
        "ನೀವು ಹೇಳಿದ್ದು ಗಂಭೀರವಾಗಿರಬಹುದು. ದಯವಿಟ್ಟು ತಡ ಮಾಡದೆ ವೈದ್ಯರನ್ನು "
        "ಸಂಪರ್ಕಿಸಿ. (Some of what you describe can be serious — please seek "
        "medical care promptly.)"
    ),
    "ml": (
        "നിങ്ങൾ പറഞ്ഞത് ഗുരുതരമായിരിക്കാം. ദയവായി വൈകാതെ ഡോക്ടറെ കാണുക. "
        "(Some of what you describe can be serious — please seek medical "
        "care promptly.)"
    ),
    "gu": (
        "તમે જે જણાવ્યું તે ગંભીર હોઈ શકે છે. કૃપા કરીને વિલંબ કર્યા વિના "
        "ડૉક્ટરનો સંપર્ક કરો. (Some of what you describe can be serious — "
        "please seek medical care promptly.)"
    ),
    "pa": (
        "ਤੁਸੀਂ ਜੋ ਦੱਸਿਆ ਉਹ ਗੰਭੀਰ ਹੋ ਸਕਦਾ ਹੈ। ਕਿਰਪਾ ਕਰਕੇ ਦੇਰ ਕੀਤੇ ਬਿਨਾਂ "
        "ਡਾਕਟਰ ਨਾਲ ਸੰਪਰਕ ਕਰੋ। (Some of what you describe can be serious — "
        "please seek medical care promptly.)"
    ),
}

_SCOPE: dict[str, str] = {
    "en": SCOPE_DECLINE,
    "hi": (
        "मैं केवल स्वास्थ्य से जुड़े सवालों में मदद कर सकता हूँ। क्या आपकी "
        "सेहत से जुड़ा कोई सवाल है?"
    ),
    "hi-Latn": (
        "Main sirf health se jude sawalon mein madad kar sakta hoon. Kya "
        "aapki sehat se juda koi sawal hai?"
    ),
    "te": (
        "నేను ఆరోగ్య సంబంధిత ప్రశ్నలకు మాత్రమే సహాయం చేయగలను. మీ ఆరోగ్యానికి "
        "సంబంధించిన ప్రశ్న ఏదైనా ఉందా?"
    ),
    "ta": (
        "நான் உடல்நலம் தொடர்பான கேள்விகளுக்கு மட்டுமே உதவ முடியும். உங்கள் "
        "உடல்நலம் பற்றி ஏதேனும் கேள்வி உள்ளதா?"
    ),
    "bn": (
        "আমি শুধুমাত্র স্বাস্থ্য সম্পর্কিত প্রশ্নে সাহায্য করতে পারি। আপনার "
        "স্বাস্থ্য নিয়ে কোনো প্রশ্ন আছে কি?"
    ),
    "mr": (
        "मी फक्त आरोग्याशी संबंधित प्रश्नांमध्ये मदत करू शकतो. आपल्या "
        "आरोग्याबद्दल काही प्रश्न आहे का?"
    ),
    "kn": (
        "ನಾನು ಆರೋಗ್ಯ ಸಂಬಂಧಿತ ಪ್ರಶ್ನೆಗಳಿಗೆ ಮಾತ್ರ ಸಹಾಯ ಮಾಡಬಲ್ಲೆ. ನಿಮ್ಮ "
        "ಆರೋಗ್ಯದ ಬಗ್ಗೆ ಯಾವುದಾದರೂ ಪ್ರಶ್ನೆ ಇದೆಯೇ?"
    ),
    "ml": (
        "എനിക്ക് ആരോഗ്യ സംബന്ധമായ ചോദ്യങ്ങളിൽ മാത്രമേ സഹായിക്കാൻ കഴിയൂ. "
        "നിങ്ങളുടെ ആരോഗ്യത്തെക്കുറിച്ച് എന്തെങ്കിലും ചോദ്യമുണ്ടോ?"
    ),
    "gu": (
        "હું માત્ર આરોગ્ય સંબંધિત પ્રશ્નોમાં મદદ કરી શકું છું. તમારા "
        "સ્વાસ્થ્ય વિશે કોઈ પ્રશ્ન છે?"
    ),
    "pa": (
        "ਮੈਂ ਸਿਰਫ਼ ਸਿਹਤ ਨਾਲ ਜੁੜੇ ਸਵਾਲਾਂ ਵਿੱਚ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ। ਕੀ ਤੁਹਾਡੀ "
        "ਸਿਹਤ ਬਾਰੇ ਕੋਈ ਸਵਾਲ ਹੈ?"
    ),
}


_SELF_HARM: dict[str, str] = {
    "en": (
        "I'm really glad you told me — what you're feeling matters. You are "
        "not alone, and support is available right now: please call the "
        "Tele-MANAS mental-health helpline at 14416 (toll-free, 24x7, in your "
        "language), or your local emergency number if you are in immediate "
        "danger. If you can, reach out to someone you trust and let them know "
        "how you're feeling. Talking to a mental-health professional can "
        "genuinely help."
    ),
    "hi": (
        "आपने बताया, यह बहुत अच्छा किया — आपकी भावनाएँ मायने रखती हैं। आप "
        "अकेले नहीं हैं। कृपया अभी Tele-MANAS हेल्पलाइन 14416 (टोल-फ्री, "
        "24x7) पर कॉल करें, या तुरंत खतरे में हों तो अपना स्थानीय आपातकालीन "
        "नंबर मिलाएँ। किसी भरोसेमंद व्यक्ति से भी बात करें।"
    ),
    "hi-Latn": (
        "Aapne bataya, yeh bahut accha kiya — aapki feelings maayne rakhti "
        "hain. Aap akele nahi hain. Kripya abhi Tele-MANAS helpline 14416 "
        "(toll-free, 24x7) par call karein, ya turant khatre mein hon to "
        "apna local emergency number milayein. Kisi bharosemand vyakti se "
        "bhi baat karein."
    ),
    "te": (
        "మీరు చెప్పినందుకు ధన్యవాదాలు — మీ భావాలు ముఖ్యం. మీరు ఒంటరి కాదు. "
        "దయచేసి ఇప్పుడే Tele-MANAS హెల్ప్‌లైన్ 14416 (టోల్-ఫ్రీ, 24x7, మీ "
        "భాషలో)కి కాల్ చేయండి; తక్షణ ప్రమాదంలో ఉంటే మీ లోకల్ ఎమర్జెన్సీ "
        "నంబర్‌కు కాల్ చేయండి. మీరు నమ్మే వ్యక్తితో కూడా మాట్లాడండి."
    ),
    "ta": (
        "நீங்கள் சொன்னது நல்லது — உங்கள் உணர்வுகள் முக்கியம். நீங்கள் "
        "தனியாக இல்லை. தயவு செய்து இப்போதே Tele-MANAS உதவி எண் 14416 "
        "(கட்டணமில்லா, 24x7, உங்கள் மொழியில்) அழைக்கவும்; உடனடி ஆபத்தில் "
        "இருந்தால் உள்ளூர் அவசர எண்ணை அழைக்கவும். நம்பகமான ஒருவரிடமும் "
        "பேசுங்கள்."
    ),
    "bn": (
        "আপনি বলেছেন, খুব ভালো করেছেন — আপনার অনুভূতি গুরুত্বপূর্ণ। আপনি "
        "একা নন। অনুগ্রহ করে এখনই Tele-MANAS হেল্পলাইন 14416 (টোল-ফ্রি, "
        "24x7, আপনার ভাষায়) কল করুন; তাৎক্ষণিক বিপদে থাকলে স্থানীয় জরুরি "
        "নম্বরে কল করুন। বিশ্বস্ত কারো সাথেও কথা বলুন।"
    ),
    "mr": (
        "आपण सांगितले, हे खूप चांगले केले — आपल्या भावना महत्त्वाच्या आहेत. "
        "आपण एकटे नाही. कृपया आत्ताच Tele-MANAS हेल्पलाइन 14416 (टोल-फ्री, "
        "24x7, आपल्या भाषेत) वर कॉल करा; तात्काळ धोका असल्यास स्थानिक "
        "आपत्कालीन क्रमांक लावा. विश्वासू व्यक्तीशीही बोला."
    ),
    "kn": (
        "ನೀವು ಹೇಳಿದ್ದು ಒಳ್ಳೆಯದು — ನಿಮ್ಮ ಭಾವನೆಗಳು ಮುಖ್ಯ. ನೀವು ಒಬ್ಬಂಟಿಯಲ್ಲ. "
        "ದಯವಿಟ್ಟು ಈಗಲೇ Tele-MANAS ಸಹಾಯವಾಣಿ 14416 (ಟೋಲ್-ಫ್ರೀ, 24x7, ನಿಮ್ಮ "
        "ಭಾಷೆಯಲ್ಲಿ)ಗೆ ಕರೆ ಮಾಡಿ; ತಕ್ಷಣದ ಅಪಾಯದಲ್ಲಿದ್ದರೆ ಸ್ಥಳೀಯ ತುರ್ತು "
        "ಸಂಖ್ಯೆಗೆ ಕರೆ ಮಾಡಿ. ನಂಬಿಕೆಯ ವ್ಯಕ್ತಿಯೊಂದಿಗೂ ಮಾತನಾಡಿ."
    ),
    "ml": (
        "നിങ്ങൾ പറഞ്ഞത് നല്ലതാണ് — നിങ്ങളുടെ വികാരങ്ങൾ പ്രധാനമാണ്. നിങ്ങൾ "
        "ഒറ്റയ്ക്കല്ല. ദയവായി ഇപ്പോൾ തന്നെ Tele-MANAS ഹെൽപ്പ്‌ലൈൻ 14416 "
        "(ടോൾ-ഫ്രീ, 24x7, നിങ്ങളുടെ ഭാഷയിൽ) വിളിക്കുക; ഉടനടി അപകടത്തിലാണെങ്കിൽ "
        "ലോക്കൽ എമർജൻസി നമ്പർ വിളിക്കുക. വിശ്വസിക്കുന്ന ഒരാളോടും സംസാരിക്കുക."
    ),
    "gu": (
        "તમે જણાવ્યું, ખૂબ સારું કર્યું — તમારી લાગણીઓ મહત્વની છે. તમે "
        "એકલા નથી. કૃપા કરીને હમણાં જ Tele-MANAS હેલ્પલાઇન 14416 (ટોલ-ફ્રી, "
        "24x7, તમારી ભાષામાં) પર કૉલ કરો; તાત્કાલિક જોખમમાં હો તો સ્થાનિક "
        "ઈમરજન્સી નંબર લગાવો. વિશ્વાસુ વ્યક્તિ સાથે પણ વાત કરો."
    ),
    "pa": (
        "ਤੁਸੀਂ ਦੱਸਿਆ, ਬਹੁਤ ਚੰਗਾ ਕੀਤਾ — ਤੁਹਾਡੀਆਂ ਭਾਵਨਾਵਾਂ ਮਾਇਨੇ ਰੱਖਦੀਆਂ ਹਨ। "
        "ਤੁਸੀਂ ਇਕੱਲੇ ਨਹੀਂ ਹੋ। ਕਿਰਪਾ ਕਰਕੇ ਹੁਣੇ Tele-MANAS ਹੈਲਪਲਾਈਨ 14416 "
        "(ਟੋਲ-ਫ੍ਰੀ, 24x7, ਤੁਹਾਡੀ ਭਾਸ਼ਾ ਵਿੱਚ) ਤੇ ਕਾਲ ਕਰੋ; ਤੁਰੰਤ ਖਤਰੇ ਵਿੱਚ ਹੋ "
        "ਤਾਂ ਲੋਕਲ ਐਮਰਜੈਂਸੀ ਨੰਬਰ ਮਿਲਾਓ। ਭਰੋਸੇਯੋਗ ਵਿਅਕਤੀ ਨਾਲ ਵੀ ਗੱਲ ਕਰੋ।"
    ),
}


def _lookup(table: dict[str, str], lang: str) -> str:
    """Exact language first, then the base language for romanized variants
    ("te-Latn" → "te" — every native entry carries an English gloss where it
    matters), then English."""
    if lang in table:
        return table[lang]
    base = lang.split("-", 1)[0]
    if base in table:
        return table[base]
    return table["en"]


def localized_self_harm(lang: str) -> str:
    return _lookup(_SELF_HARM, lang)


def localized_emergency(lang: str) -> str:
    return _lookup(_EMERGENCY, lang)


def localized_high_escalation(lang: str) -> str:
    return _lookup(_HIGH, lang)


def localized_scope_decline(lang: str) -> str:
    return _lookup(_SCOPE, lang)
