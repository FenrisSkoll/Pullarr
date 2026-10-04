"""Managed catalog evidence is corroboration, never publication substitution."""
from dataclasses import replace
from unittest import TestCase

from Tbackend.features.identification import candidate, comic, issue, volume

from backend.base.identification import MatchState, PublicationAuthority
from backend.implementations.identification import (MatchingSnapshot,
                                                    identify_authorized)


class Matching152Tests(TestCase):
    def check(self,title,series,subtitle,stem,number=1,serial=False):
        c=candidate(number=None,stem=stem)
        c=replace(c,filename=replace(c.filename,series=title,volume_number=number))
        c=comic(c,f'<Series>{series}</Series><Title>{subtitle}</Title><Volume>{number}</Volume>')
        catalog=[issue(n,str(n),float(n),title=('Ordinary serial story' if serial else ('Volume One' if n==1 else f'Volume {n}'))) for n in (1,2,3)]
        snapshot=MatchingSnapshot.build([volume(title=title)],catalog)
        return identify_authorized(c,snapshot,1,PublicationAuthority.MANAGED_VOLUME)

    def test_black_road_split_title(self):
        r=self.check('Black Road: The Holy North','Black Road','The Holy North','Black Road - The Holy North (2016) - v001')
        self.assertEqual(r.state,MatchState.AUTOMATIC)
        self.assertEqual(r.selected.local_issue_ids,(1,))

    def test_collected_catalog(self):
        for n in (1,2,3):
            r=self.check('Army of Darkness Omnibus','Army of Darkness Omnibus',f'Army of Darkness Omnibus Vol. {n:02}',f'Army of Darkness Omnibus (2010) - v{n:03}',n)
            self.assertEqual(r.state,MatchState.AUTOMATIC)
            self.assertEqual(r.selected.local_issue_ids,(n,))

    def test_serial_volume_is_not_issue(self):
        r=self.check('Batman','Batman','Story','Batman (2020) - v001',serial=True)
        self.assertEqual(r.state,MatchState.REVIEW)
        self.assertEqual(r.selected.local_issue_ids,())

    def test_conflicting_collected_title_is_not_accepted(self):
        r=self.check('Batman','Batman','Volume 3','Batman (2020) - v001')
        self.assertEqual(r.state,MatchState.REVIEW)
