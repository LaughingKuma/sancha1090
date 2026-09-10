{# 5C callsign variants: one physical flight is spelled two ways across sources (JL45/JAL45, KAL0017/KAL017,
   JAL46D/JAL46), so anchor compat compares flight NUMBERS -- the digits themselves never waive. #}
{# Anchor anti-joins ONLY: the cluster/merge rule and the attach seam compare exact by design (v6.16
   splits rotations on a callsign difference). Unrelated to callsign_norm, which keys vrs schedule votes. #}

{# NULL or decode garbage -> compatible-with-anything in compat tests. Pure-alpha registrations
   ('VHPYN') are real callsigns, so junk needs a missing letter, a non-[A-Z0-9] char, or one repeated char. #}
{% macro callsign_is_junk(expr) -%}
{%- set t = 'upper(trimBoth(' ~ expr ~ '))' -%}
({{ expr }} is null
 or not match({{ t }}, '^[A-Z0-9]*[A-Z][A-Z0-9]*$')
 or {{ t }} = repeat(substring({{ t }}, 1, 1), length({{ t }})))
{%- endmacro %}

{# The flight number two spellings must agree on, NULL when there is none. Digit-leading IATA prefixes exist
   ('5J5108' -> 5108, never 5) so it is the LAST digit run, zero-stripped ('KAL0017' -> '17'). #}
{% macro callsign_flight_num(expr) -%}
{%- set t = 'upper(trimBoth(' ~ expr ~ '))' -%}
{#- Letters after the run are the number too ('N681HC' -> '681HC', 'FIN7LP' -> '7LP'): a registration or an
    alphanumeric flight ID is not flight 681 (#225). A lone suffix letter still drops ('JAL46D' -> '46'). -#}
{%- set tail = "extract(" ~ t ~ ", '[0-9]+[A-Z]{2,}$')" -%}
{%- set run = "ifNull(nullIf(" ~ tail ~ ", ''), arrayElement(extractAll(" ~ t ~ ", '[0-9]+'), -1))" -%}
{%- set stripped = "replaceRegexpOne(" ~ run ~ ", '^0+', '')" -%}
if({{ callsign_is_junk(expr) }} or not match({{ t }}, '[0-9]'),
   NULL,
   if({{ stripped }} = '', '0', {{ stripped }}))
{%- endmacro %}
