"""Applicant Info 存草稿这一步的契约。

两条都是 2026-08-03 用真实账号实测踩出来的，且都属于「HTTP 200 但其实失败」
这一类——不看响应体根本发现不了。

1. **页面 JS 不设的字段就别自己编。** 点 Save 时 ``onclickfunctions('Save')``
   只动三个字段：``myButtonClicked`` / ``ContentclassName`` / ``FormError``。
   它**从不碰** ``IsSave`` 和 ``SaveContinueClicked``（页面默认都是空串）。
   实现里凭空发了 ``IsSave="1"``，服务端就切到「提交申请」的校验路径，回
   「Please upload required documents before Proceeding.」。

2. **必须验收响应。** 服务端拒绝时照样回 200，错误只藏在响应体的
   ``$.showMessage({type:"error",…})`` 里。不检查就会把「什么都没存下」报成
   「已为你起草申请，请迅速上传证件」——用户会安心去准备证件，等发现时房子
   已经没了。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe import RentCafeSaveRejectedError, RentCafeSession

#: 实测抄录：真实 ApplicantInfo 页上这几个字段的默认值。
PAGE = (
    '<form id="ApplicantInformation" name="ApplicantInformation">'
    '<input type="hidden" name="formName2" value="ApplicantInformation">'
    '<input type="hidden" name="ContentclassName" value="ApplicantInformation">'
    '<input type="hidden" name="myButtonClicked" value="Save">'
    '<input type="hidden" name="IsSave" value="">'
    '<input type="hidden" name="SaveContinueClicked" value="">'
    '<input type="hidden" name="FormError" value="">'
    '<input type="hidden" name="isDocumentSetupAvailbleAtThisStep" value="0">'
    "</form>"
)

REJECTED = (
    '<script type="text/javascript">(function($) {$(function () { '
    'DecodeFormElementsToBase64(); $.showMessage({type: "error",'
    'text:"Please upload required documents before Proceeding.",time:5000}); '
    "}); })(jQuery); </script>"
)


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.headers = {}


class _Session(RentCafeSession):
    def __init__(self, reply=""):
        self._base_url = "https://h.test"
        self._ole_path = "/onlineleasing/p"
        self._post_url = "https://h.test/onlineleasing/rcformsave.ashx"
        self._cafeportalkey = "CPK-1"
        self._impersonate = "chrome124"
        self._source = "xior"
        self._last_html = ""
        self.posted: dict = {}
        self._reply = reply

    def _post(self, url, data, referer=""):
        self.posted = dict(data)
        return _Resp(self._reply)

    def _get(self, url):
        return _Resp("")


class TestDoesNotInventFields:
    def test_does_not_send_is_save(self):
        s = _Session()
        s.save_applicant_info(PAGE, {"FirstName": "A"})
        assert "IsSave" not in s.posted, (
            "页面 JS 从不设 IsSave；发 IsSave='1' 会让服务端要求先传证件"
        )

    def test_does_not_send_save_continue_clicked(self):
        s = _Session()
        s.save_applicant_info(PAGE, {"FirstName": "A"})
        assert "SaveContinueClicked" not in s.posted

    def test_sends_the_three_fields_the_page_actually_sets(self):
        s = _Session()
        s.save_applicant_info(PAGE, {"FirstName": "A"})
        assert s.posted["myButtonClicked"] == "Save"
        assert s.posted["ContentclassName"] == "ApplicantInformation"
        assert s.posted["FormError"] == "0"

    def test_profile_fields_reach_the_form(self):
        s = _Session()
        s.save_applicant_info(PAGE, {"FirstName": "A", "LastName": "B"})
        assert s.posted["FirstName"] == "A"
        assert s.posted["LastName"] == "B"


class TestVerifiesTheServerAccepted:
    def test_rejection_raises_instead_of_returning_quietly(self):
        with pytest.raises(RentCafeSaveRejectedError, match="upload required documents"):
            _Session(REJECTED).save_applicant_info(PAGE, {"FirstName": "A"})

    def test_empty_reply_is_success(self):
        """和站内其它表单一样：没有错误要显示，返回的就是空的。"""
        _Session("").save_applicant_info(PAGE, {"FirstName": "A"})

    def test_rejection_is_not_a_generic_error(self):
        """要能和「被封」「凭据错」区分开——这三种给用户的话完全不同。"""
        from bookers.rentcafe import RentCafeAuthError, RentCafeBlockedError

        assert not issubclass(RentCafeSaveRejectedError, RentCafeBlockedError)
        assert not issubclass(RentCafeSaveRejectedError, RentCafeAuthError)
