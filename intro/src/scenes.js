import towns from '../../assets/images/towns/credits.json'
import home from '../../assets/images/home/credits.json'

/* The scenes of the intro: one site photograph each, with a two-part headline (a bold sans line, then an
   italic serif line) and one line of small print. The pictures are the site's own, in assets/images,
   and each shows the credit its licence asks for. Every claim is something the site really does.
   An entry with `finale` has no photograph behind it: the aerial photograph shows through the letters. */

export const SCENE = 3.4          // seconds per scene

const credits = new Map([...towns, ...home].map((e) => [e.file, e.credit]))
const photo = (file) => ({ src: `../images/${file}`, credit: credits.get(`assets/images/${file}`) })

export const SCENES = [
  { ...photo('towns/jurong-east.jpg'), pos: '50% 62%',
    lead: 'Every resale since 1990,', tail: 'in one place.',
    note: '985,219 real sales, January 1990 to September 2026.' },
  { ...photo('home/void-deck-on-the-ground-floor-of.jpg'), pos: '50% 55%',
    lead: 'Every price,', tail: 'in context.',
    note: 'Judged against flats of the same type sold in the same town and quarter.' },
  { ...photo('towns/choa-chu-kang.jpg'), pos: '50% 50%',
    lead: 'How far to', tail: 'the MRT.',
    note: 'The straight-line distance to the nearest MRT or LRT station, for every block.' },
  { ...photo('towns/bishan.jpg'), pos: '40% 45%',
    lead: 'What’s around', tail: 'the block.',
    note: 'Parks, hawker centres, gyms, childcare centres and schools nearby.' },
  { ...photo('towns/clementi.jpg'), pos: '50% 55%',
    lead: 'Save the flats', tail: 'you like.',
    note: 'Shortlist them and compare side by side, with an account.' },
  { ...photo('home/prices.jpg'), finale: true, lead: 'ResaleWise', tail: 'HDB resale prices, made clearer.' },
]
