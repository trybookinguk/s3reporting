Attribute VB_Name = "BoxOfficeRules"
' Box Office Terminals — business rules (the bits a formula can't enforce).
'
' Ported from the reporting-dashboard box-office feature:
'   - double-booking guard: a terminal can't be on two overlapping ACTIVE hires,
'     UNLESS one continues from the other (the kit never came back).
'   - continuation: when a hire's ContinuesFromHireId is set, the prior hire is
'     marked 'completed' and contact/address/dates carry over.
'   - trial flag bypasses the payment-required rule.
'
' Design for Excel: ONE terminal per hire row. Single-owner editing.
'
' Paste this whole module into the VBA editor (Alt+F11 -> right-click the
' VBAProject -> Import File... -> BoxOfficeRules.bas). Then add the small
' Worksheet_Change hook from HiresSheet.cls to the Hires sheet's code.

Option Explicit

Public Const HIRES_TABLE As String = "HiresTable"

' Status sets (keep in step with Lists / the app)
Private Function IsActive(ByVal status As String) As Boolean
    Select Case LCase$(status)
        Case "confirmed", "shipped", "in_use": IsActive = True
        Case Else: IsActive = False
    End Select
End Function

Private Function IsEnded(ByVal status As String) As Boolean
    Select Case LCase$(status)
        Case "returned", "completed", "cancelled": IsEnded = True
        Case Else: IsEnded = False
    End Select
End Function

' --- table access helpers -------------------------------------------------
Private Function HiresLO() As ListObject
    Set HiresLO = ThisWorkbook.Worksheets("Hires").ListObjects(HIRES_TABLE)
End Function

Private Function ColIndex(lo As ListObject, ByVal header As String) As Long
    ColIndex = lo.ListColumns(header).Index
End Function

' Whole-day overlap of [aFrom,aTo] and [bFrom,bTo]; empty 'to' = open-ended.
Private Function RangesOverlap(aFrom As Date, aTo As Variant, _
                               bFrom As Date, bTo As Variant) As Boolean
    Dim aEnd As Date, bEnd As Date
    aEnd = IIf(IsDate(aTo), CDate(aTo), DateSerial(9999, 12, 31))
    bEnd = IIf(IsDate(bTo), CDate(bTo), DateSerial(9999, 12, 31))
    RangesOverlap = (aFrom <= bEnd) And (bFrom <= aEnd)
End Function

' --- the double-booking guard --------------------------------------------
' Returns "" if OK, or an error message if the row would double-book a terminal.
Public Function ValidateHireRow(ByVal rowIndex As Long) As String
    Dim lo As ListObject: Set lo = HiresLO()
    Dim cId As Long, cAcct As Long, cStatus As Long, cTrial As Long
    Dim cFrom As Long, cTo As Long, cTerm As Long, cCont As Long
    Dim cPay As Long
    cId = ColIndex(lo, "HireId"): cStatus = ColIndex(lo, "Status")
    cTrial = ColIndex(lo, "IsTrial"): cFrom = ColIndex(lo, "HireFrom")
    cTo = ColIndex(lo, "HireTo"): cTerm = ColIndex(lo, "Terminal")
    cCont = ColIndex(lo, "ContinuesFromHireId"): cPay = ColIndex(lo, "PaymentReceived")

    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)
    Dim myId As String, myStatus As String, myTerm As String, myCont As String
    myId = CStr(r.Cells(1, cId).Value)
    myStatus = CStr(r.Cells(1, cStatus).Value)
    myTerm = CStr(r.Cells(1, cTerm).Value)
    myCont = CStr(r.Cells(1, cCont).Value)

    If Trim$(myTerm) = "" Then Exit Function           ' no terminal yet — nothing to check
    If Not IsActive(myStatus) Then Exit Function        ' only active hires can clash

    If Not IsDate(r.Cells(1, cFrom).Value) Then
        ValidateHireRow = "HireFrom must be a date."
        Exit Function
    End If
    Dim myFrom As Date: myFrom = CDate(r.Cells(1, cFrom).Value)
    Dim myTo As Variant: myTo = r.Cells(1, cTo).Value

    Dim i As Long, other As Range
    For i = 1 To lo.DataBodyRange.Rows.Count
        If i <> rowIndex Then
            Set other = lo.DataBodyRange.Rows(i)
            Dim oId As String, oTerm As String, oStatus As String, oCont As String
            oId = CStr(other.Cells(1, cId).Value)
            oTerm = CStr(other.Cells(1, cTerm).Value)
            oStatus = CStr(other.Cells(1, cStatus).Value)
            oCont = CStr(other.Cells(1, cCont).Value)

            If oTerm = myTerm And IsActive(oStatus) Then
                ' Skip the linked continuation partner (kit never came back).
                If Not (oId = myCont Or myId = oCont) Then
                    If IsDate(other.Cells(1, cFrom).Value) Then
                        If RangesOverlap(myFrom, myTo, _
                                         CDate(other.Cells(1, cFrom).Value), _
                                         other.Cells(1, cTo).Value) Then
                            ValidateHireRow = "Terminal '" & myTerm & _
                                "' is already on an active, overlapping hire (" & _
                                CStr(other.Cells(1, ColIndex(lo, "AccountName")).Value) & _
                                "). Link it as a continuation if the kit stayed out."
                            Exit Function
                        End If
                    End If
                End If
            End If
        End If
    Next i

    ' Trial bypasses the payment requirement; otherwise active needs payment.
    If Not CBool(r.Cells(1, cTrial).Value) Then
        If Not CBool(r.Cells(1, cPay).Value) Then
            ValidateHireRow = "Status '" & myStatus & _
                "' needs PaymentReceived = TRUE (or tick IsTrial)."
        End If
    End If
End Function

' --- continuation: complete the prior hire + carry details over -----------
Public Sub ApplyContinuationForRow(ByVal rowIndex As Long)
    Dim lo As ListObject: Set lo = HiresLO()
    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)
    Dim cId As Long, cCont As Long
    cId = ColIndex(lo, "HireId"): cCont = ColIndex(lo, "ContinuesFromHireId")
    Dim priorId As String: priorId = CStr(r.Cells(1, cCont).Value)
    If Trim$(priorId) = "" Then Exit Sub

    ' Find the prior hire row.
    Dim i As Long, prior As Range
    For i = 1 To lo.DataBodyRange.Rows.Count
        If CStr(lo.DataBodyRange.Rows(i).Cells(1, cId).Value) = priorId Then
            Set prior = lo.DataBodyRange.Rows(i)
            Exit For
        End If
    Next i
    If prior Is Nothing Then Exit Sub

    ' Carry over contact + address; outbound = collect (already on site).
    CopyCell prior, r, lo, "ContactName"
    CopyCell prior, r, lo, "ContactEmail"
    CopyCell prior, r, lo, "ContactPhone"
    CopyCell prior, r, lo, "ShippingAddress"
    CopyCell prior, r, lo, "AccountId"
    CopyCell prior, r, lo, "AccountName"
    r.Cells(1, ColIndex(lo, "OutboundMethod")).Value = "collect"

    ' Start the day after the prior ends (or today if open-ended).
    Dim pTo As Variant: pTo = prior.Cells(1, ColIndex(lo, "HireTo")).Value
    Dim newFrom As Date
    newFrom = IIf(IsDate(pTo), CDate(pTo) + 1, Date)
    r.Cells(1, ColIndex(lo, "HireFrom")).Value = newFrom
    ' If a stale HireTo now precedes the new start, clear it.
    Dim myTo As Variant: myTo = r.Cells(1, ColIndex(lo, "HireTo")).Value
    If IsDate(myTo) Then
        If CDate(myTo) < newFrom Then r.Cells(1, ColIndex(lo, "HireTo")).Value = ""
    End If

    ' Close out the prior hire unless already ended.
    Dim pStatus As String: pStatus = CStr(prior.Cells(1, ColIndex(lo, "Status")).Value)
    If Not IsEnded(pStatus) Then
        prior.Cells(1, ColIndex(lo, "Status")).Value = "completed"
        prior.Cells(1, ColIndex(lo, "ChangedAt")).Value = Now
    End If
End Sub

Private Sub CopyCell(src As Range, dst As Range, lo As ListObject, header As String)
    Dim c As Long: c = ColIndex(lo, header)
    dst.Cells(1, c).Value = src.Cells(1, c).Value
End Sub

' --- stamp a new HireId / audit on a fresh row ----------------------------
Public Sub StampRow(ByVal rowIndex As Long)
    Dim lo As ListObject: Set lo = HiresLO()
    Dim r As Range: Set r = lo.DataBodyRange.Rows(rowIndex)
    Dim cId As Long: cId = ColIndex(lo, "HireId")
    If Trim$(CStr(r.Cells(1, cId).Value)) = "" Then
        r.Cells(1, cId).Value = "H" & Format(Now, "yyyymmddhhnnss") & _
                                Right$("000" & rowIndex, 3)
    End If
    r.Cells(1, ColIndex(lo, "ChangedAt")).Value = Now
    If Trim$(CStr(r.Cells(1, ColIndex(lo, "ChangedBy")).Value)) = "" Then
        r.Cells(1, ColIndex(lo, "ChangedBy")).Value = Environ$("Username")
    End If
End Sub
