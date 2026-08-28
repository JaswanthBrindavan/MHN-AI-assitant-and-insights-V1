"""Native-script + romanized red-flag phrases for the non-Hindi Indian
languages the product supports.

DRAFT — machine-authored, pending native-speaker AND clinician review, like
the Hindi rows in red_flags.py that set the pattern. Matching is the same
normalized substring pass; single short romanized tokens are screened out at
generation time because a substring matcher cannot afford them.

Regenerate: the authoring workflow output is committed as the source of
truth for review; edit rows here directly as reviewers correct them.
"""

from __future__ import annotations

I18N_EMERGENCY_PHRASES: tuple[str, ...] = (
    # --- Targeted supplements from live red-team testing (DRAFT). The very
    # case that exposed this gap: "naaku oopiri adakatledu" (Telugu, romanized:
    # "I can't breathe") received a please-rephrase reply and NO escalation.
    # Stems chosen so common spelling variants match by substring.
    "oopiri ada",        # te-Latn: breath not coming ("oopiri adakatledu"/"adatledu")
    "oopiri aada",       # te-Latn variant
    "upiri ada",         # te-Latn variant
    "oopiri raavatledu",  # te-Latn: breath is not coming
    "moochu vida mudiya",  # ta-Latn: cannot breathe ("mudiyala"/"mudiyavillai")
    "moochu vara illai",   # ta-Latn: breath not coming
    "mucchu vida mudiya",  # ta-Latn spelling variant
    # --- Bengali (bn) ---
    "অজ্ঞান হয়ে",  # has fallen unconscious (stem  matches '  /')
    "oggan hoye",  # unconscious  common phonetic typing (matches 'og
    "ogyan hoye",  # unconscious  variant romanization of 
    "শ্বাস নিতে পারছি না",  # I can't breathe (also matched inside '   ')
    "শ্বাস নিতে পারছে না",  # he/she can't breathe (third person  how family m
    "shash nite parchi na",  # I can't breathe (also matched inside 'nishash ni
    "shash nite parche na",  # he/she can't breathe (third person)
    "শ্বাস বন্ধ হয়ে",  # breathing has stopped / is stopping (stem  match
    "shash bondho hoye",  # breathing has stopped (stem)
    "খিঁচুনি",  # seizure / fits (matches ' /')
    "খিচুনি",  # seizure  very common typing without chandrabindu
    "khichuni",  # seizure / fits
    "গলায় আটকে",  # choking  stuck in the throat (matches '  /')
    "golay atke",  # choking  stuck in the throat
    "মুখ বেঁকে",  # face has twisted/drooped  the lay Bengali stroke
    "mukh beke",  # face drooped/twisted  lay stroke sign (covers bo
    "হার্ট অ্যাটাক হচ্ছে",  # heart attack is happening now (present-tense anc
    "heart attack hocche",  # heart attack happening right now (past 'hoyechil
    # --- Gujarati (gu) ---
    "બેભાન",  # unconscious / unresponsive  catches ' ', '  ', '
    "શ્વાસ નથી",  # can't breathe  catches '  ', '  ', '  '
    "શ્વાસ બંધ",  # breathing has stopped  '    '
    "ગળામાં ફસા",  # choking  something stuck in the throat; stem cat
    "ખેંચ આવ",  # seizure / fits  '  ', ' '; NOTE for review: also
    "હાર્ટ એટેક",  # heart attack  the English loanword as actually t
    "લકવો",  # stroke  the lay 'paralysis' word (' ', '  '); si
    "ઢળી પડ",  # collapsed  stem catches ' //' (male/female/plura
    "છાતીમાં સખત દુખા",  # severe chest pain right now  '    '
    "shwas nathi",  # can't breathe  'shwas nathi aavto', 'shwas nathi
    "swas nathi",  # can't breathe  common alternate romanization of 
    "attack aav",  # '(heart) attack came'  Gujaratis say ' ' meaning
    "khench aav",  # seizure / fits  'khench aave che', 'khench aavi'
    # --- Kannada (kn) ---
    "ಉಸಿರಾಡೋಕೆ ಆಗ್ತಿಲ್ಲ",  # can't breathe (colloquial first person)
    "ಉಸಿರಾಡುತ್ತಿಲ್ಲ",  # is not breathing (said about a person)
    "usiradoke agtilla",  # can't breathe
    "usiradakke agtilla",  # can't breathe (spelling variant)
    "usiru bartilla",  # breath is not coming / can't breathe
    "ಪ್ರಜ್ಞೆ ತಪ್ಪಿ",  # lost consciousness (stem: matches tappide / tapp
    "prajne tappi",  # lost consciousness (stem)
    "pragne tappi",  # lost consciousness (common romanization variant)
    "ಎಚ್ಚರ ತಪ್ಪಿ",  # fainted / lost consciousness (stem; safer than '
    "ಫಿಟ್ಸ್ ಬಂದ",  # seizure / fits happening ('fits' is the lay word
    "fits bandide",  # having a seizure / fits
    "ಹೃದಯಾಘಾತ",  # heart attack (unambiguous medical term, also mat
    "ಹಾರ್ಟ್ ಅಟ್ಯಾಕ್",  # heart attack (English loan written in Kannada sc
    "ಲಕ್ವ",  # paralysis / stroke (lay term; prefix also substr
    "lakva hod",  # stroke / paralysis has struck (stem: hodedide / 
    "ಕುಸಿದು ಬಿದ್ದ",  # collapsed and fell (stem covers biddaru / biddan
    "ಗಂಟಲಲ್ಲಿ ಸಿಕ್ಕಿ",  # stuck in the throat / choking (stem: sikkide / s
    "ಎದೆ ನೋವು ಜಾಸ್ತಿ",  # chest pain is severe  'heart pain right now' phr
    "ede novu jasti",  # chest pain is severe (substring also catches the
    # --- Malayalam (ml) ---
    "ബോധം പോയി",  # lost consciousness / passed out
    "bodham poyi",  # lost consciousness / passed out (romanized)
    "കുഴഞ്ഞു വീണു",  # collapsed / crumpled and fell (spaced spelling)
    "കുഴഞ്ഞുവീണു",  # collapsed / crumpled and fell (compound spelling
    "kuzhanju veenu",  # collapsed / crumpled and fell (romanized)
    "ശ്വാസം കിട്ടുന്നില്ല",  # can't breathe / not getting breath
    "shwasam kittunnilla",  # can't breathe ('shw' + double-n spelling)
    "swasam kittunilla",  # can't breathe ('sw' + single-n spelling, common 
    "ശ്വാസം നിലച്ചു",  # breathing has stopped
    "shwasam nilachu",  # breathing has stopped (romanized)
    "തൊണ്ടയിൽ കുടുങ്ങി",  # stuck in the throat  choking
    "thondayil kudungi",  # stuck in the throat  choking (romanized)
    "അപസ്മാരം",  # seizure / epileptic fit  single word but disease
    "fits vannu",  # 'fits came'  had a seizure; the common Manglish 
    "ഹാർട്ട് അറ്റാക്ക്",  # heart attack  English loan typed in Malayalam sc
    "അറ്റാക്ക് വന്നു",  # 'attack came'  in Kerala lay usage this means a 
    "attack vannu",  # 'attack came'  heart attack (romanized); also ca
    "വശം തളർന്നു",  # one side of the body went limp/paralyzed  the la
    "vasham thalarnnu",  # one side went limp  lay stroke (romanized)
    "നെഞ്ചിൽ ഭയങ്കര വേദന",  # terrible pain in the chest  'heart pain right no
    "nenjil bhayankara vedana",  # terrible chest pain right now (romanized)
    # --- Marathi (mr) ---
    "बेशुद्ध पडल",  # fell unconscious / is unresponsive (verb stem: m
    "beshudh pad",  # fell unconscious (romanized; covers 'beshudh pad
    "श्वास घेता येत नाही",  # cannot breathe (also matches '... ')
    "shwas gheta yet",  # cannot breathe (covers 'shwas gheta yet nahi' / 
    "swas gheta yet",  # cannot breathe (common WhatsApp spelling without
    "श्वास थांबल",  # breathing has stopped (stem: matches thambla/tha
    "shwas thambl",  # breathing has stopped (covers 'shwas thambla', '
    "घशात अडकल",  # something is stuck in the throat  choking (stem:
    "ghashat adakl",  # choking  stuck in throat (covers 'ghashat adakla
    "फिट आली",  # had a seizure / fit (the common lay word; 'fit' 
    "zatke yet",  # having convulsions (covers 'zatke yet ahet', 'za
    "हार्ट अटॅक आल",  # heart attack  the lay phrase Marathi speakers ac
    "heart attack ala",  # heart attack happening/happened (English noun + 
    "अर्धांगवायूचा झटका",  # stroke  paralytic attack (common household term)
    "paralysis attack",  # stroke  the Indian lay term 'paralysis attack' a
    "अचानक कोसळल",  # suddenly collapsed (stem: kosalla/kosalli  male/
    # --- Punjabi (pa) ---
    "ਬੇਹੋਸ਼",  # unconscious  bare distinctive word (precedent: H
    "ਸਾਹ ਨਹੀਂ ਆ",  # can't breathe  deliberate stem: as a substring i
    "ਸਾਹ ਰੁਕ ਗਿਆ",  # breathing has stopped ('saah' is masculine, so t
    "ਦੌਰਾ ਪੈ",  # seizure/fit striking  stem catches '  ' and '  '
    "ਦਿਲ ਦਾ ਦੌਰਾ",  # heart attack  THE lay phrase in Punjabi
    "ਲਕਵਾ",  # stroke / paralysis attack  bare distinctive medi
    "ਗਲੇ ਵਿੱਚ ਫਸ",  # choking  something stuck in the throat; stem end
    "sah nahi aa",  # can't breathe (right now)  catches 'sah nahi aa 
    "saah nahi aa",  # can't breathe  long-vowel 'saah' spelling varian
    "daura pai",  # seizure/fit struck  'daura pai gya/gia' (Punjabi
    "dil da daura",  # heart attack  lay phrase, Punjabi genitive 'da' 
    "dil da dora",  # heart attack  very common chat romanization 'dor
    "gale ch fas",  # choking  'gale ch fas gya/gayi' (chat Punjabi us
    # --- Tamil (ta) ---
    "மூச்சு விட முடிய",  # can't breathe  stem catches    /  (colloquial) a
    "மூச்சு வரல",  # breath is not coming (colloquial: 'moochu varala
    "மூச்சு நின்",  # breathing has stopped  stem catches  /  / ()
    "மயங்கி விழுந்",  # fainted and fell / collapsed  stem catches  /  /
    "மயக்கம் போட்டு",  # fainted (lit. 'put a faint')  catches  / 
    "சுயநினைவு இல்ல",  # unconscious / no consciousness  catches  and 
    "பேச்சு மூச்ச",  # unresponsive  the idiom '  ' (no speech, no brea
    "வலிப்பு வந்த",  # seizure struck  catches  /  / 
    "வலிப்பு வருது",  # is having fits / convulsing right now
    "தொண்டையில் மாட்டி",  # something stuck in the throat  choking (standard
    "மாரடைப்பு",  # heart attack  THE lay term; single specific word
    "ஹார்ட் அட்டாக்",  # 'heart attack' typed in Tamil script  common tra
    "நெஞ்சு அடைக்",  # chest is blocking/tightening  classic acute MI p
    "பக்கவாதம்",  # stroke / paralytic attack (the lay word)
    "வாய் கோணி",  # mouth has turned crooked  the acute stroke sign 
    "moochu vida mudiya",  # can't breathe  stem catches mudiyala / mudiyalai
    "muchu vida mudiya",  # can't breathe  'muchu' spelling variant of 
    "moochu varala",  # breath is not coming
    "moochu nin",  # breathing stopped  stem catches ninnu / ninnuduc
    "pechu moochu illa",  # unresponsive  'no speech, no breath'; catches il
    "mayangi vizhun",  # fainted and fell / collapsed  stem catches vizhu
    "mayangi vilun",  # fainted and fell   romanized as 'l' (vilunthu / 
    "mayakkam pottu",  # fainted (lit. 'put a faint')  catches pottutaar 
    "valippu van",  # seizure struck  stem catches vanthu / vandhu / v
    "valippu varu",  # having fits right now  stem catches varuthu / va
    "thondaila maatti",  # stuck in the throat  choking (colloquial locativ
    "maradaippu",  # heart attack (lay term )
    "maradaipu",  # heart attack  single-p spelling variant
    "nenju adaik",  # chest is blocking/tightening  stem catches adaik
    "pakkavatham",  # stroke / paralytic attack
    "pakkavadham",  # stroke  'dh' spelling variant
    "vaai koni",  # mouth turned crooked  acute stroke sign ('vaai k
    # --- Telugu (te) ---
    "ఊపిరి ఆడటం లేదు",  # breath is not coming  can't breathe
    "ఊపిరి ఆడట్లేదు",  # can't breathe (colloquial contraction as typed i
    "oopiri aadatam ledu",  # can't breathe
    "upiri adatam ledu",  # can't breathe (short-vowel romanization)
    "ఊపిరి ఆగిపోయింది",  # breathing has stopped
    "oopiri aagipoyindi",  # breathing has stopped
    "స్పృహ తప్పింది",  # lost consciousness / fainted
    "spruha tappindi",  # lost consciousness
    "spruha ledu",  # no consciousness  unresponsive
    "గొంతులో ఇరుక్కుంది",  # stuck in the throat  choking
    "gonthulo irukkundi",  # choking  stuck in the throat
    "gontulo irukkundi",  # choking (spelling variant without h)
    "ఫిట్స్ వచ్చ",  # fits/seizure (stem  matches ' ' and ' ')
    "fits vachayi",  # had fits  seizure (plural agreement)
    "fits vastunnayi",  # fits are happening right now
    "మూర్ఛ",  # seizure / fainting fit (distinctive word, preced
    "moorcha vachindi",  # had a seizure / fainting fit
    "గుండెపోటు",  # heart attack (the common lay word)
    "gundepotu",  # heart attack
    "gunde potu",  # heart attack (spaced romanization)
    "పక్షవాతం",  # paralysis / stroke (the common lay word)
    "pakshavatam",  # stroke  paralysis attack
    "pakshavatham",  # stroke (common 'th' romanization)
    "నోరు వంకర",  # mouth turned crooked  facial droop, lay stroke s
    "noru vankara",  # mouth crooked  facial droop
    "కళ్ళు తిరిగి పడి",  # went dizzy and collapsed (stem  matches  male / 
    "kallu tirigi padi",  # dizzy and collapsed (stem  matches padipoyadu ma
    "గుండె నొప్పి",  # heart pain  cardiac-type chest pain right now (T
    "gunde noppi",  # heart pain right now (also matches 'gunde noppig
    "గుండెల్లో నొప్పి",  # pain in the heart/chest  lay 'heart pain' phrasi
    "gundello noppi",  # pain in the heart/chest
)

I18N_HIGH_PHRASES: tuple[str, ...] = (
    # --- Bengali (bn) ---
    "বুকে প্রচণ্ড ব্যথা",  # severe chest pain
    "বুকে খুব ব্যথা",  # very bad chest pain ( is the stable everyday int
    "buke khub betha",  # very bad chest pain
    "buke onek betha",  # a lot of chest pain (common WhatsApp phrasing)
    "রক্ত বমি",  # vomiting blood (matches '  /')
    "rokto bomi",  # vomiting blood
    "কাশির সাথে রক্ত",  # blood with cough  coughing up blood
    "kashir sathe rokto",  # coughing up blood
    "মুখ দিয়ে রক্ত",  # blood coming from the mouth (stem  matches // ; 
    "mukh diye rokto",  # blood coming from the mouth
    "শ্বাস নিতে খুব কষ্ট",  # severe difficulty breathing ( anchors severity s
    "shash nite khub koshto",  # severe difficulty breathing
    "পেটে খুব ব্যথা",  # severe stomach/abdominal pain
    "pete khub betha",  # severe stomach pain
    # --- Gujarati (gu) ---
    "છાતીમાં દુખ",  # chest pain  stem catches '  ' and ' '
    "લોહીની ઉલટી",  # vomiting blood  '   '
    "ઉલટીમાં લોહી",  # blood in the vomit  word-order variant of the ab
    "ઉધરસમાં લોહી",  # coughing blood  '   ' ( = cough)
    "શ્વાસ લેવામાં તકલીફ",  # difficulty breathing  '    '
    "પેટમાં સખત દુખા",  # severe stomach pain  '  '
    "પેટમાં બહુ દુખે",  # stomach hurts a lot  the colloquial severe-stoma
    "chati ma dukh",  # chest pain  stem catches 'chati ma dukhe che', '
    "chhati ma dukh",  # chest pain  'chhati' spelling variant (very comm
    "lohi ni ulti",  # vomiting blood  'lohi ni ulti thay che'
    "ulti ma lohi",  # blood in the vomit  word-order variant
    "khansi ma lohi",  # coughing blood  the Hindi-loan 'khansi' is what 
    "shwas chade",  # breathless / severe breathing difficulty  'shwas
    "pet ma bahu dukhe",  # stomach hurts a lot  severe stomach pain, colloq
    # --- Kannada (kn) ---
    "ಎದೆ ನೋವು",  # chest pain
    "ede novu",  # chest pain (substring also catches the 'yede nov
    "ರಕ್ತ ವಾಂತಿ",  # vomiting blood
    "rakta vanti",  # vomiting blood
    "raktha vanthi",  # vomiting blood (aspirated th romanization, very 
    "blood vanti",  # vomiting blood (Kanglish mix people actually typ
    "ಕೆಮ್ಮಿನಲ್ಲಿ ರಕ್ತ",  # blood in the cough / coughing blood
    "kemmalli rakta",  # blood in the cough (colloquial locative 'kemmall
    "ಉಸಿರಾಟದ ತೊಂದರೆ",  # breathing difficulty
    "usirata tondare",  # breathing difficulty
    "usiru kashta",  # breathing is hard / difficult ('usiru kashta agt
    "ಹೊಟ್ಟೆ ನೋವು ಜಾಸ್ತಿ",  # stomach pain is severe (intensity qualifier keep
    "hotte novu jasti",  # severe stomach pain
    "tumba hotte novu",  # very bad stomach pain (qualifier-first word orde
    # --- Malayalam (ml) ---
    "നെഞ്ചുവേദന",  # chest pain (compound spelling)
    "നെഞ്ച് വേദന",  # chest pain (spaced spelling)
    "nenju vedana",  # chest pain (romanized)
    "nenjil vedana",  # pain in the chest (romanized locative form  'nen
    "nenju vedhana",  # chest pain (romanized, common 'vedhana' spelling
    "രക്തം ഛർദ്ദി",  # vomiting blood  verb stem, catches chardichu (pa
    "raktham chardi",  # vomiting blood (romanized stem  catches chardich
    "chora chardi",  # vomiting blood, colloquial 'chora' for blood (ro
    "കഫത്തിൽ രക്തം",  # blood in the phlegm/sputum  the common lay phras
    "kaphathil raktham",  # blood in the phlegm (romanized; 'kabathil'/'kafa
    "ചോര തുപ്പുന്നു",  # spitting up blood (colloquial)
    "ശ്വാസം മുട്ടുന്നു",  # severe breathlessness / gasping (spaced spelling
    "ശ്വാസംമുട്ട",  # breathlessness  compound stem, catches shvasammu
    "shwasam mutt",  # breathless (romanized stem  catches muttal / mut
    "swasam mutt",  # breathless (romanized stem, 'sw' spelling)
    "വയറ്റിൽ ഭയങ്കര വേദന",  # terrible pain in the stomach  severe stomach pai
    "bhayankara vayaru vedana",  # terrible stomach pain (romanized)
    # --- Marathi (mr) ---
    "छातीत दुखत",  # chest is hurting (stem: matches dukhtay / dukhat
    "chatit dukh",  # chest pain (covers 'chatit dukhtay', 'chatit duk
    "chest madhe dukh",  # chest hurting  the very common mixed English-Mar
    "रक्ताची उलटी",  # vomiting blood (singular: 'raktachi ulti zali')
    "रक्ताच्या उलट्या",  # vomiting blood repeatedly (plural: 'raktachya ul
    "raktachi ulti",  # vomiting blood (romanized, singular)
    "raktachya ultya",  # vomiting blood (romanized, plural  'raktachya ul
    "खोकल्यातून रक्त",  # blood coming out while coughing ('khoklyatun rak
    "khoklyatun rakt",  # coughing up blood (stem 'rakt' also matches the 
    "श्वास घ्यायला त्रास",  # difficulty breathing ('shwas ghyayla tras hotoy'
    "shwas ghyayla tras",  # difficulty breathing (romanized)
    "swas ghyayla tras",  # difficulty breathing (common spelling variant wi
    "पोटात खूप दुखत",  # severe stomach pain ('potat khup dukhtay'; 'khup
    "potat khup dukh",  # severe stomach pain (covers 'potat khup dukhtay'
    # --- Punjabi (pa) ---
    "ਛਾਤੀ ਵਿੱਚ ਬਹੁਤ ਦਰਦ",  # very bad chest pain  uses '' rather than '' beca
    "ਹਿੱਕ ਵਿੱਚ ਦਰਦ",  # chest pain  '' is the colloquial/rural Punjabi w
    "ਖੂਨ ਦੀ ਉਲਟੀ",  # vomiting blood  Punjabi genitive '' (the Hindi r
    "ਖੰਘ ਵਿੱਚ ਖੂਨ",  # blood in the cough  '' is the Punjabi word for c
    "ਸਾਹ ਲੈਣ ਵਿੱਚ ਬਹੁਤ ਤਕਲੀਫ",  # severe difficulty breathing  row ends at  (no nu
    "ਢਿੱਡ ਵਿੱਚ ਬਹੁਤ ਦਰਦ",  # severe belly pain  '' is the everyday Punjabi wo
    "chhati ch tez dard",  # severe chest pain  'ch' is the dominant chat spe
    "chhati vich bahut dard",  # very bad chest pain  'vich' spelling variant (do
    "hik ch dard",  # chest pain  colloquial 'hik' = chest; the space-
    "khoon di ulti",  # vomiting blood  Punjabi 'di' (the existing 'khoo
    "khun di ulti",  # vomiting blood  short-vowel 'khun' spelling vari
    "khang ch khoon",  # coughing blood / blood in the cough ('khang' = c
    "dhid ch bahut dard",  # severe belly pain  'dhid' = belly, the phrasing 
    "pet ch bahut dard",  # severe stomach pain  'pet' loanword form; full p
    # --- Tamil (ta) ---
    "நெஞ்சு வலி",  # chest pain  catches   /  ( is a prefix of the ve
    "நெஞ்சுவலி",  # chest pain written as one word
    "ரத்த வாந்தி",  # vomiting blood  also substring-matches formal   
    "ரத்தம் கக்க",  # spitting/bringing up blood  stem catches  /  / 
    "இருமலில் ரத்த",  # blood in the cough  catches  after 
    "மூச்சு திணற",  # severe breathing difficulty / gasping  stem catc
    "மூச்சு வாங்கு",  # gasping for breath, badly breathless  catches  /
    "வயிறு வலி தாங்க",  # unbearable stomach pain  catches   /  / 
    "வயித்து வலி தாங்க",  # unbearable stomach pain  colloquial  spelling
    "nenju vali",  # chest pain  catches 'nenju valikuthu' / 'nenju v
    "ratha vanthi",  # vomiting blood
    "ratha vandhi",  # vomiting blood  'dhi' spelling variant
    "blood vanthi",  # vomiting blood  Tanglish mix people really type
    "ratham kak",  # spitting up blood  stem catches kakkuthu / kakut
    "irumina ratham",  # blood when I cough ('irumina ratham varuthu')
    "cough la blood",  # blood in the cough  very common Tanglish constru
    "moochu thinar",  # severe breathing difficulty  stem catches thinar
    "moochu vangu",  # gasping / badly breathless  catches vanguthu / v
    "vayiru vali thanga",  # unbearable stomach pain  catches 'thanga mudiyal
    "vayithu vali thanga",  # unbearable stomach pain  colloquial 'vayithu' sp
    # --- Telugu (te) ---
    "ఛాతీలో నొప్పి",  # chest pain (anatomical chest  'chaati')
    "chest lo noppi",  # chest pain (code-mixed English+Telugu, the most 
    "chaati lo noppi",  # chest pain
    "రక్తం వాంతులు",  # vomiting blood (matches inside '  ')
    "వాంతిలో రక్తం",  # blood in the vomit
    "raktam vantulu",  # vomiting blood
    "raktham vanthulu",  # vomiting blood (common 'th' romanization)
    "vantilo raktam",  # blood in the vomit
    "దగ్గులో రక్తం",  # blood in the cough  coughing blood
    "daggulo raktam",  # blood in the cough
    "daggite raktam",  # blood when coughing ('daggite raktam vastundi')
    "ఊపిరి తీసుకోవడం కష్టం",  # very hard to breathe  severe breathing difficult
    "oopiri teesukovadam kastam",  # hard to take a breath  severe breathing difficul
    "ఆయాసం ఎక్కువ",  # breathlessness is severe (matches '  ')
    "ayasam ekkuva",  # severe breathlessness
    "కడుపులో విపరీతమైన నొప్పి",  # severe stomach pain
    "kadupu noppi ekkuva",  # stomach pain is severe (matches 'kadupu noppi ek
    "kadupu noppi tattukoleka",  # unbearable stomach pain (matches 'tattukoleka po
)

I18N_SELF_HARM_PHRASES: tuple[str, ...] = (
    # --- Bengali (bn) ---
    "মরে যেতে চাই",  # I want to die
    "মরতে ইচ্ছে",  # feel like dying (stem  matches '  /')
    "more jete chai",  # I want to die
    "morte icche",  # feel like dying (matches 'morte icche korche')
    "morte chai",  # want to die (short form people actually type)
    "বাঁচতে চাই না",  # I don't want to live (negative particle included
    "বাঁচতে ইচ্ছে করে না",  # don't feel like living anymore
    "bachte chai na",  # don't want to live (covers both  and  typings)
    "আত্মহত্যা",  # suicide (the formal word, mirrors the Hindi  row
    "সুইসাইড",  # suicide  English loanword typed in Bengali scrip
    "নিজেকে শেষ করে",  # end/finish myself (matches '   /')
    "nijeke shesh kore",  # end myself
    "নিজের ক্ষতি করতে",  # to harm myself ( anchors intent  'smoking harms 
    "nijer khoti korte",  # to harm myself (intent form)
    # --- Gujarati (gu) ---
    "મરી જવું છે",  # 'I want to die'  '   '; infinitive is gender-neu
    "મરવું છે",  # '(I) want to die'  '  ', the shorter form people
    "જીવવું નથી",  # 'I don't want to live'  '  '
    "જીવન ટૂંકાવ",  # 'end my life'  the standard Gujarati lay phrase 
    "આપઘાત",  # suicide  the common Gujarati word ('    '); sing
    "આત્મહત્યા",  # suicide  the formal word, also widely typed
    "કંઈક કરી લઈશ",  # veiled self-harm: 'I will do something (to mysel
    "marvu che",  # 'want to die'  'mare marvu che'
    "marvu chhe",  # 'want to die'  'chhe' spelling variant of 
    "mari javu",  # 'want to die away'  catches 'mari javu che' and 
    "jivvu nathi",  # 'don't want to live'  'mare jivvu nathi'
    "jivan tunkav",  # 'end my life'  'jivan tunkavi dais', 'jivan tunk
    "kaink kari lais",  # veiled: 'I'll do something to myself'  also catc
    # --- Kannada (kn) ---
    "ಸಾಯಬೇಕು",  # want to / should die (catches 'sayabeku anisutti
    "ಬದುಕೋಕೆ ಇಷ್ಟ ಇಲ್ಲ",  # don't want to live
    "ಆತ್ಮಹತ್ಯೆ",  # suicide (the word people actually type; substrin
    "ಜೀವನ ಮುಗಿಸ",  # end my life (stem: mugisabeku / mugisibidabeku /
    "ಸತ್ತು ಹೋಗಬೇಕು",  # want to die away (the -beku intent form; deliber
    "ಕೈ ಕೊಯ್ದುಕೊಳ್ಳ",  # cut my own wrist/hand (intent-form stem; does no
    "sayabeku",  # want to / should die
    "badukoke ishta illa",  # don't want to live
    "atmahaty",  # suicide (stem catches atmahatya / atmahatye / aa
    "jeevana mugis",  # end my life (stem)
    "life mugis",  # end my life (Kanglish 'life mugisbeku'; safe  in
    "sattu hogbeku",  # want to die (colloquial contraction)
    "sattu hogabeku",  # want to die (full-vowel spelling variant)
    "kai koydkobeku",  # want to cut my wrist/hand (intent form only, avo
    # --- Malayalam (ml) ---
    "മരിക്കണം",  # '(I) want to die'  the phrase distressed people 
    "marikkanam",  # want to die (romanized)
    "marikanam",  # want to die (romanized, single-k WhatsApp spelli
    "ജീവിക്കണ്ട",  # 'don't want to live'  very common ideation phras
    "jeevikkanda",  # don't want to live (romanized)
    "ചത്താൽ മതി",  # 'it's enough if I die' / better off dead  raw co
    "chathal mathi",  # better off dead (romanized)
    "chathaal mathi",  # better off dead (romanized, 'aa' spelling)
    "ആത്മഹത്യ",  # suicide  the word people actually type; catches 
    "athmahathya",  # suicide (romanized)
    "atmahathya",  # suicide (romanized, 't' spelling)
    "ജീവിതം മടുത്തു",  # fed up with life / tired of living  common depre
    "jeevitham maduthu",  # fed up with life (romanized)
    "ജീവിതം അവസാനിപ്പിക്ക",  # end (my) life  verb stem, catches avasanippikkan
    "jeevitham avasanipp",  # end my life (romanized stem  catches all tense e
    "ഞരമ്പ് മുറി",  # cut the (wrist) vein  the Kerala lay phrase for 
    "njarambu muri",  # cut the wrist vein (romanized stem)
    "narambu muri",  # cut the wrist vein (romanized, 'n' spelling)
    # --- Marathi (mr) ---
    "मरावंसं वाट",  # I feel like dying (stem: matches 'maravansa vata
    "मला मरायच",  # I want to die (matches 'mala maraychay', 'mala m
    "maravasa vat",  # feel like dying (covers 'maravasa vatatay', 'mar
    "maravas vat",  # feel like dying (spelling variant without traili
    "marun jav",  # want to die / be dead ('marun javasa vatatay', '
    "mala maraych",  # I want to die ('mala maraychay', 'mala maraycha 
    "जीव द्याव",  # feel like ending my life ('jeev dyavasa vatatoy'
    "jeev dyava",  # ending my life (covers 'jeev dyava vatato', 'jee
    "आयुष्य संपव",  # end my life (stem: matches 'ayushya sampvavasa v
    "life sampv",  # end my life  very common mixed typing ('life sam
    "आत्महत्य",  # suicide (stem: matches 'aatmahatya', 'aatmahatye
    "स्वतःला इजा",  # hurting myself / harming myself ('swatahala ija 
    "जगायचं नाही",  # I do not want to live ('mala jagaycha nahiye')
    "jagaycha nahi",  # do not want to live ('mala jagaycha nahi/nahiye'
    # --- Punjabi (pa) ---
    "ਮਰਨਾ ਚਾਹੁੰਦ",  # want to die  stem ends before the gender vowel s
    "ਜੀਣਾ ਨਹੀਂ ਚਾਹੁੰਦ",  # don't want to live  same gender-neutral stem tri
    "ਖੁਦਕੁਸ਼ੀ",  # suicide  the word people actually type in Gurmuk
    "ਆਤਮ ਹੱਤਿਆ",  # suicide  formal word; spaced form ('' alone is e
    "ਆਪਣੇ ਆਪ ਨੂੰ ਨੁਕਸਾਨ",  # harm to myself  Punjabi dative '' (the Hindi row
    "ਫਾਹਾ ਲੈ",  # take the noose / hang myself  the common Punjabi
    "marna chau",  # want to die  catches 'marna chaunda' (m) and 'ma
    "marna chah",  # want to die  catches the 'marna chahunda/chahund
    "mar jana chau",  # want to die  'mar jana chaunda/chaundi', the oth
    "jeena nahi chau",  # don't want to live  Punjabi 'chaunda/chaundi'; N
    "jina ni chau",  # don't want to live  colloquial short spelling ('
    "khud nu nuksan",  # harm myself  Punjabi 'nu' (existing rows only co
    "apne aap nu khatam",  # end/finish myself  'apne aap nu khatam kar lena'
    "faha le",  # hang myself  noose idiom, 'faha le lena/faha le 
    # --- Tamil (ta) ---
    "தற்கொலை",  # suicide  the word people actually type; catches 
    "சாகணும்",  # I want to die ('  ')  some hyperbolic use exists
    "செத்துடணும்",  # I want to just die  intent form chosen over the 
    "செத்துடலாம்",  # might as well die ('  ')
    "உயிரை மாய்",  # end my life  catches  / ; chosen over ' ' which 
    "வாழ்க்கையை முடி",  # end my life  stem catches  /  / 
    "வாழ பிடிக்கல",  # I don't want to live / don't like living
    "வாழவே பிடிக்கல",  # emphatic: don't want to live at all  the extra  
    "தூக்கு போட",  # hang myself  catches   /  / 
    "விஷம் குடி",  # drink poison  catches  /  / 
    "கையை அறுத்து",  # cut my hand/wrist deliberately (transitive , dis
    "tharkolai",  # suicide ()  catches 'tharkolai pannikalam' etc.
    "thatkolai",  # suicide   romanized as 't' variant
    "seththuda",  # double-t spelling variant (seththudanum / sethth
    "vazha pidik",  # don't want to live  stem catches pidikala / pidi
    "vazha pudik",  # don't want to live  'pudi' spelling (how it's ac
    "vazhave pidik",  # emphatic: don't want to live at all ('vazhave pi
    "life ah mudi",  # end my life  Tanglish; stem catches 'life ah mud
    "thooku pot",  # hang myself  catches pottu / pottukanum / potuka
    "thooku pod",  # hang myself  catches podalam / podanum / podren
    "visham kudi",  # drink poison  catches kudichiduven / kudikalam
    "poison kudi",  # drink poison  Tanglish mix ('poison kudichiduven
    "kaiya aru",  # cut my hand/wrist  stem catches aruthukitten / a
    "blade pottuk",  # cut myself with a blade  the phrasing teens use 
    # --- Telugu (te) ---
    "చచ్చిపోవాలని",  # wanting to die (stem  matches '...' and '...')
    "chachipovalani",  # wanting to die (stem  matches 'chachipovalani un
    "sachipovalani",  # wanting to die (Telangana s- dialect spelling)
    "చనిపోవాలని",  # wanting to die (softer verb 'chanipovu')
    "chanipovalani",  # wanting to die
    "బతకాలని లేదు",  # don't want to live (negative kept  bare stem wou
    "batakalani ledu",  # don't want to live
    "bratakalani ledu",  # don't want to live (common br- spelling)
    "ఆత్మహత్య",  # suicide (the word people actually type in Telugu
    "atmahatya",  # suicide (already present in the Hindi rows  kept
    "చంపుకుంటా",  # I will kill myself (reflexive  matches '')
    "champukunta",  # I will kill myself (matches 'champukuntanu')
    "champeskunta",  # I will kill myself (colloquial 'champesukunta' c
    "చంపేసుకుంటా",  # I will kill myself (colloquial emphatic form)
    "ఉరి వేసుకుంటా",  # I will hang myself
    "uri vesukunta",  # I will hang myself (matches 'uri vesukuntanu')
    "uri veskunta",  # I will hang myself (texting contraction)
)
